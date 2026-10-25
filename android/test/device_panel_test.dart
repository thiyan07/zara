import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/device_panel.dart';

Map<String, dynamic> snapWithDevices() => {
      'supported': true,
      'trust_note': 'detection is not trust',
      'devices': [
        {
          'src': 'storage_volume',
          'id': 'storage-1234',
          'label': 'SanDisk USB drive',
          'stat': {'total_bytes': 64023257088, 'free_bytes': 64000000000},
          'removable': true,
        },
        {
          'src': 'usb',
          'id': 'usb-1050:2011',
          'label': 'Some gadget',
          'class_hint': 'hid',
        },
        {
          'src': 'display',
          'id': 'display-2',
          'label': 'Cast display',
          'width_px': 1920,
          'height_px': 1080,
        },
      ],
    };

void main() {
  testWidgets('panel shows only supported actions per device class',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
      body: DeviceObservedPanel(snapshot: snapWithDevices(), onAction: (_) {}),
    )));
    // pendrive: usage + media stats + copy + move visible; verified by
    // discovered capability, not a generic list.
    expect(find.text('Usage'), findsOneWidget);
    expect(find.text('Media stats'), findsOneWidget);
    expect(find.text('Copy here'), findsOneWidget);
    // HID gadget: honest "no supported actions yet" — no fake buttons.
    expect(find.text('no supported actions yet'), findsOneWidget);
    // display: report button (state report is genuinely implemented).
    expect(find.text('Resolution/mode'), findsOneWidget);
    // trust boundary always visible.
    expect(find.textContaining('Detection is not trust'), findsOneWidget);
  });

  testWidgets('unsupported build shows honest abstain, never panels',
      (tester) async {
    await tester.pumpWidget(const MaterialApp(
        home: Scaffold(body: DeviceObservedPanel(
            snapshot: {'supported': false}))));
    expect(find.textContaining('not supported'), findsOneWidget);
    expect(find.text('Usage'), findsNothing);
    expect(find.text('Media stats'), findsNothing);
  });

  testWidgets('empty snapshot shows honest empty state', (tester) async {
    await tester.pumpWidget(const MaterialApp(
        home: Scaffold(
            body: DeviceObservedPanel(
                snapshot: {'supported': true, 'devices': []}))));
    expect(find.textContaining('No separate devices'), findsOneWidget);
  });

  testWidgets('action taps carry device id and action', (tester) async {
    final seen = <String, String>{};
    await tester.pumpWidget(MaterialApp(
        home: Scaffold(
            body: DeviceObservedPanel(
      snapshot: snapWithDevices(),
      onAction: (cmd) => seen[cmd['action'] as String] =
          cmd['device'] as String,
    ))));
    await tester.tap(find.text('Usage'));
    await tester.pump();
    expect(seen['storage.volumes'], equals('storage-1234'));
  });
}
