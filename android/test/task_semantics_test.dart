import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/main.dart';

// Semantics + stable keys for the Tasks/fleet/approval controls, so
// screen readers and UI automation identify them without coordinate
// taps. No Core needed: the form renders its controls unconditionally.

void main() {
  const deviceChannel = MethodChannel('zara/device');
  const secureChannel =
      MethodChannel('plugins.it_nomads.com/flutter_secure_storage');

  SemanticsHandle? semHandle;

  setUp(() {
    TestWidgetsFlutterBinding.ensureInitialized();
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(
            deviceChannel, (call) async => <String, dynamic>{});
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(secureChannel, (call) async => null);
  });

  tearDown(() {
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(deviceChannel, null);
    TestWidgetsFlutterBinding.instance.defaultBinaryMessenger
        .setMockMethodCallHandler(secureChannel, null);
  });

  Future<void> openTasks(WidgetTester t) async {
    await t.pumpWidget(const ZaraApp());
    await t.pumpAndSettle();
    await t.tap(find.text('Tasks'));
    await t.pumpAndSettle();
  }

  // Widget tests don't build a semantics tree unless asked; enable it
  // so label assertions exercise the real tree screen readers see.
  void enableSemantics(WidgetTester t) {
    semHandle = t.ensureSemantics();
  }

  /// Every label in the live semantics tree (newline-flattened), for
  /// substring assertions that don't depend on matcher exactness rules.
  List<String> semanticsLabels(WidgetTester t) {
    final out = <String>[];
    void walk(SemanticsNode n) {
      if (n.label.isNotEmpty) out.add(n.label.replaceAll('\n', ' '));
      n.visitChildren((c) {
        walk(c);
        return true;
      });
    }

    walk(t.getSemantics(find.byType(Scaffold)));
    return out;
  }

  group('tasks controls are keyed and labelled', () {
    testWidgets('submit button found by key with button semantics',
        (t) async {
      enableSemantics(t);
      await openTasks(t);
      expect(find.byKey(const ValueKey('task-submit')), findsOneWidget);
      expect(find.bySemanticsLabel('Submit task'), findsOneWidget);
      semHandle?.dispose();
      semHandle = null;
    });

    testWidgets('target dropdown exposes its fleet label', (t) async {
      enableSemantics(t);
      await openTasks(t);
      // InputDecoration labelText lands in the semantics tree; match
      // loosely since value/hint text joins the same node.
      expect(
          semanticsLabels(t).any((l) => l.contains('Run on (fleet)')),
          isTrue);
      semHandle?.dispose();
      semHandle = null;
    });

    testWidgets('resolve toggle exposes its label', (t) async {
      enableSemantics(t);
      await openTasks(t);
      expect(
          semanticsLabels(t)
              .any((l) => l.contains('Route via capability (resolve)')),
          isTrue);
      semHandle?.dispose();
      semHandle = null;
    });

    testWidgets('empty task list shows an explicit empty state', (t) async {
      await openTasks(t);
      expect(
          find.textContaining('No tasks yet', skipOffstage: false),
          findsOneWidget);
    });
  });
}
