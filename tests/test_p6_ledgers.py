"""Chat ledgers (P6-b28) — parsing, writes, honesty, reports, and the
flow gate. Deterministic: injected clock, in-memory DB unless the
persistence test uses tmp_path. No currency is invented; no unit is
merged; "remind"-shaped asks never land here."""
from core.ledgers import (LedgerService, LedgerFlow, LedgerValueError,
                          handle_ledger_utterance, parse_ledger_intent)


class ListAudit:
    def __init__(self):
        self.entries = []

    def record(self, source, action, target="", detail=""):
        self.entries.append((source, action, target, detail))


def _svc():
    a = ListAudit()
    return LedgerService(audit=a), a


# ---------------- gate ----------------

def test_gate_and_exclusions():
    f = LedgerFlow(service=_svc()[0])
    assert f.wants_turn("add water 2 glasses")
    assert f.wants_turn("add expense 250 for coffee")
    assert f.wants_turn("add milk to my grocery list")
    assert f.wants_turn("what did i eat today")
    assert not f.wants_turn("remind me to take my medication at 9 PM")
    assert not f.wants_turn("show my reminders")
    assert not f.wants_turn("what's on my calendar today")
    assert f.wants_turn("show my ledger")


# ---------------- water ----------------

def test_water_shapes():
    op = parse_ledger_intent("add water 2 glasses")
    assert op["op"] == "water" and op["amount"] == 2 \
        and op["unit"] == "glass"
    op = parse_ledger_intent("I drank 3 glasses of water")
    assert op["op"] == "water" and op["amount"] == 3
    op = parse_ledger_intent("water 500 ml")
    assert op["op"] == "water" and op["unit"] == "ml"
    op = parse_ledger_intent("add water 2")
    assert op["op"] == "clarify" and "glasses" in op["message"]


def test_water_log_and_total():
    svc, _ = _svc()
    assert "3 glass" in handle_ledger_utterance(
        svc, "add water 3 glasses")["message"]
    assert "2 bottle" in handle_ledger_utterance(
        svc, "i drank 2 bottles of water")["message"]
    rep = handle_ledger_utterance(svc, "how much water today")
    assert "3 glass" in rep["message"] and "2 bottle" in \
        rep["message"]
    m = handle_ledger_utterance(svc, "add water 0 glasses")
    assert m["refused"] is True


# ---------------- expenses ----------------

def test_expense_shapes():
    op = parse_ledger_intent("add expense 250 for coffee with sam")
    assert op["op"] == "expense" and op["amount"] == 250 and \
        "coffee" in op["note"]
    op2 = parse_ledger_intent("spent 60 on lunch")
    assert op2["op"] == "expense" and op2["amount"] == 60 \
        and op2["note"] == "lunch"


def test_expense_log_and_total():
    svc, _ = _svc()
    handle_ledger_utterance(svc, "add expense 250 for coffee")
    handle_ledger_utterance(svc, "spent 60 on lunch")
    rep = handle_ledger_utterance(svc, "expenses today")
    assert "310.00" in rep["message"]
    assert "no currency assumed" in rep["message"]


# ---------------- grocery ----------------

def test_grocery_flow():
    svc, _ = _svc()
    r = handle_ledger_utterance(svc, "add milk to my grocery list")
    assert "Added 'milk'" in r["message"]
    r = handle_ledger_utterance(svc, "add bread to my grocery list")
    assert "2 open items" in r["message"]
    dup = handle_ledger_utterance(svc, "add MILK to my grocery list")
    assert dup["refused"] is True and "already" in dup["message"]
    lst = handle_ledger_utterance(svc, "show my grocery list")
    assert "milk" in lst["message"] and "bread" in lst["message"]
    done = handle_ledger_utterance(svc, "check off milk")
    assert "checked off" in done["message"].lower()
    gone = handle_ledger_utterance(svc, "remove bread from my "
                                        "grocery list")
    assert "removed" in gone["message"].lower()
    lst2 = handle_ledger_utterance(svc, "show my grocery list")
    assert "done: milk" in lst2["message"]


def test_grocery_missing_refusal():
    svc, _ = _svc()
    r = handle_ledger_utterance(svc, "check off eggs")
    assert r["refused"] is True and "No open 'eggs'" in r["message"]


# ---------------- meal / habit / medication ----------------

def test_meal_habit_medication():
    svc, _ = _svc()
    handle_ledger_utterance(svc, "log meal idli and chutney")
    handle_ledger_utterance(svc, "i ate a banana")
    rep = handle_ledger_utterance(svc, "what did i eat today")
    assert "idli" in rep["message"] and "banana" in rep["message"]
    handle_ledger_utterance(svc, "log habit stretching done")
    rep = handle_ledger_utterance(svc, "habits today")
    assert "stretching done" in rep["message"]
    handle_ledger_utterance(svc, "took medication vitamin d")
    rep = handle_ledger_utterance(svc, "medications today")
    assert "vitamin d" in rep["message"]


# ---------------- journal + persistence ----------------

def test_journal_and_persistence():
    svc, _ = _svc()
    handle_ledger_utterance(svc, "add water 2 glasses")
    handle_ledger_utterance(svc, "add expense 20 for tea")
    j = handle_ledger_utterance(svc, "show my ledger")
    assert "water: 1" in j["message"] and "expense: 1" \
        in j["message"]


def test_persistence_across_restart(tmp_path):
    db = str(tmp_path / "ledgers.db")
    a = ListAudit()
    svc = LedgerService(audit=a, db_path=db)
    svc.log_water(2, "glass")
    svc2 = LedgerService(audit=a, db_path=db)
    es = svc2.entries(kind="water")
    assert len(es) == 1 and es[0]["amount"] == 2


# ---------------- review linkage ----------------

def test_week_line_neutral_and_real_reads():
    svc, _ = _svc()
    line = svc.week_line()
    assert "water:" in line and "no water logged" in line
    svc.log_water(6, "glass")
    line = svc.week_line()
    assert "6 glass" in line
    svc.log_expense(120, "fuel")
    line = svc.week_line()
    assert "120.00" in line or "fuel" in line


def test_unknown_shapes_fall_through():
    svc, _ = _svc()
    assert handle_ledger_utterance(svc, "play some music") is None
    assert handle_ledger_utterance(
        svc, "remind me to drink water at 6 PM") is None


# ---------------- budget (batch-32: Firefly III design, one honest
# number; no currency; the SAME expense sums) ----------------

def test_budget_set_and_status_empty_month():
    svc, _ = _svc()
    assert svc.budget_status() is None
    out = handle_ledger_utterance(svc, "set my monthly budget to 500")
    assert out["executed"] is False and "500.00" in out["message"]
    s = svc.budget_status()
    assert s["budget"] == 500.0 and s["spent"] == 0.0
    assert s["left"] == 500.0 and not s["over"]
    assert s["per_day"] == round(500 / s["days_left"], 2)


def test_budget_set_shapes_and_rounding():
    svc, _ = _svc()
    assert handle_ledger_utterance(svc, "budget 480.50 a month")
    assert svc.monthly_budget() == 480.5
    assert handle_ledger_utterance(svc, "set my monthly budget 900")
    assert svc.monthly_budget() == 900.0


def test_budget_spent_reads_the_same_expense_rows():
    svc, _ = _svc()
    handle_ledger_utterance(svc, "budget 500 a month")
    handle_ledger_utterance(svc, "add expense 45 for tea")
    handle_ledger_utterance(svc, "add expense 30.50 for parking")
    s = svc.budget_status()
    assert s["spent"] == 75.5 and s["left"] == 424.5
    # no currency invented anywhere in the honest line
    out = handle_ledger_utterance(svc, "budget status")
    assert "no currency" in out["message"]
    assert "75.50" in out["message"] and "424.50" in out["message"]


def test_budget_over_and_days_left_math():
    svc, _ = _svc()
    handle_ledger_utterance(svc, "budget 100 a month")
    svc.log_expense(160, "overshoot")
    s = svc.budget_status()
    assert s["over"] is True and s["left"] == -60.0
    out = handle_ledger_utterance(svc, "how's my budget")
    assert "over budget by 60.00" in out["message"]
    svc2, _ = _svc()
    svc2.set_monthly_budget(310)
    s2 = svc2.budget_status()
    assert s2["days_left"] >= 1
    assert s2["per_day"] == round(310 / s2["days_left"], 2)


def test_budget_clear_and_no_budget_silence():
    svc, _ = _svc()
    assert svc.budget_line() == ""        # unset → review text unchanged
    handle_ledger_utterance(svc, "budget 200 a month")
    assert svc.budget_line()
    handle_ledger_utterance(svc, "remove my budget")
    assert svc.monthly_budget() is None
    out = handle_ledger_utterance(svc, "budget status")
    assert "set my monthly budget to 500" in out["message"]
    out2 = handle_ledger_utterance(svc, "remove my budget")
    assert "nothing to clear" in out2["message"]


def test_budget_validation_honest():
    svc, _ = _svc()
    out = handle_ledger_utterance(svc, "budget 0 a month")
    assert "refused" in out
    out = handle_ledger_utterance(svc, "budget 5000000 a month")
    assert "refused" in out


def test_budget_gate_in_wants_turn():
    f = LedgerFlow(service=_svc()[0])
    assert f.wants_turn("budget status")
    assert f.wants_turn("set my monthly budget to 500")
    op = parse_ledger_intent("budget 500 a month")
    assert op["op"] == "budget_set"


def test_report_expense_ask_still_works_with_budget():
    svc, _ = _svc()
    handle_ledger_utterance(svc, "budget 300 a month")
    op = parse_ledger_intent("how much did i spend today")
    assert op["op"] == "report_expense"
    out = handle_ledger_utterance(svc, "how much did i spend today")
    assert "no expenses logged" in out["message"]
