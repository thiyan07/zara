import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/device_panel.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  testWidgets('hidden when no active mission or no data',
      (tester) async {
    await tester.pumpWidget(_wrap(
        const TransferMissionPanel(status: null, onCancel: null)));
    expect(find.byType(Card), findsNothing);
  });

  testWidgets('completed mission shows its outcome without cancel',
      (tester) async {
    await tester.pumpWidget(_wrap(const TransferMissionPanel(
      status: {
        'missions': [
          {'mission_id': 'm1', 'goal': 'copy 1 file(s)', 'state':
              'completed'},
        ],
        'transfers': <dynamic>[],
      },
      onCancel: null,
    )));
    // the last mission stays visible with its truthful outcome; no
    // cancel button on a terminal mission
    expect(find.text('Done'), findsOneWidget);
    expect(find.text('Cancel transfer'), findsNothing);
  });

  testWidgets('running mission shows progress state and cancel',
      (tester) async {
    var cancelled = false;
    await tester.pumpWidget(_wrap(TransferMissionPanel(
      status: const {
        'missions': [
          {
            'mission_id': 'm2',
            'goal': 'copy 3 file(s) from laptop to laptop:/media/p',
            'state': 'executing',
          },
        ],
        'transfers': <dynamic>[],
      },
      onCancel: () => cancelled = true,
    )));
    expect(find.byType(Card), findsOneWidget);
    expect(find.text('Transferring…'), findsOneWidget);
    expect(find.byIcon(Icons.swap_horiz), findsOneWidget);
    expect(find.text('Cancel transfer'), findsOneWidget);
    await tester.tap(find.text('Cancel transfer'));
    expect(cancelled, isTrue);
  });

  testWidgets('waiting mission surfaces the question honestly',
      (tester) async {
    await tester.pumpWidget(_wrap(const TransferMissionPanel(
      status: {
        'missions': [
          {'mission_id': 'm3', 'goal': 'copy 1 file(s)',
           'state': 'waiting'},
        ],
      },
      onCancel: null,
    )));
    expect(find.text('Waiting for your answer'), findsOneWidget);
    expect(find.byType(OutlinedButton), findsNothing); // no fake choices
  });

  testWidgets('failed mission shows partial-truth error',
      (tester) async {
    await tester.pumpWidget(_wrap(const TransferMissionPanel(
      status: {
        'missions': [
          {
            'mission_id': 'm4',
            'goal': 'copy 2 file(s)',
            'state': 'failed',
            'error': '1/2 verified — b.txt: insufficient space',
          },
        ],
      },
      onCancel: null,
    )));
    expect(find.text('Failed (partial — see details)'), findsOneWidget);
    expect(find.textContaining('1/2 verified'), findsOneWidget);
    // a failed mission still shows cancel only while non-terminal —
    // here it is terminal, so no cancel button remains truthful
    expect(find.text('Cancel transfer'), findsNothing);
  });

  test('stateLabel covers all Core states without translation slips',
      () {
    expect(TransferMissionPanel.stateLabel('executing'),
        'Transferring…');
    expect(TransferMissionPanel.stateLabel('waiting_for_permission'),
        'Waiting for approval');
    expect(TransferMissionPanel.stateLabel('cancelled'), 'Cancelled');
  });
}
