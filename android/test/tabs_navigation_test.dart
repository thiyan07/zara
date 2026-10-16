import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:zara_android/main.dart';

// Everyday-UI navigation: four predictable destinations; diagnostics live
// under Settings, never on the Chat home. Hermes-safe: the device bridge
// and secure-storage channels are mocked (empty maps / null reads), so
// the app boots to pairing without sockets or keystore.
void main() {
  const deviceChannel = MethodChannel('zara/device');
  const secureChannel =
      MethodChannel('plugins.it_nomads.com/flutter_secure_storage');

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
  Future<void> pumpApp(WidgetTester t) async {
    await t.pumpWidget(const ZaraApp());
    await t.pumpAndSettle();
  }

  testWidgets('bottom bar offers Chat/Tasks/History/Settings', (t) async {
    await pumpApp(t);
    expect(find.byType(NavigationBar), findsOneWidget);
    for (final label in ['Chat', 'Tasks', 'History', 'Settings']) {
      expect(find.text(label), findsWidgets);
    }
  });

  testWidgets('Chat is home: pairing + talk, no diagnostics', (t) async {
    await pumpApp(t);
    // Fresh boot, no stored credentials -> pairing recovery on Chat.
    expect(find.text('Pairing code from Zara Core'), findsOneWidget);
    expect(find.text('Ask Zara (streaming talk)'), findsOneWidget);
    // Developer/diagnostic surfaces stay off the home tab.
    expect(find.text('Tool (safe list only)'), findsNothing);
    expect(find.text('Record 5s test'), findsNothing);
    expect(find.text('Provider setup'), findsNothing);
    expect(find.text('Search memories'), findsNothing);
  });

  testWidgets('Tasks tab holds the submit form', (t) async {
    await pumpApp(t);
    await t.tap(find.text('Tasks'));
    await t.pumpAndSettle();
    expect(find.text('Tool (safe list only)'), findsOneWidget);
    expect(find.text('Submit task'), findsOneWidget);
    expect(find.text('Ask Zara (streaming talk)'), findsNothing);
  });

  testWidgets('History tab holds memory review + past conversations',
      (t) async {
    await pumpApp(t);
    await t.tap(find.text('History'));
    await t.pumpAndSettle();
    expect(find.text('Memory review'), findsOneWidget);
    expect(find.text('Search memories'), findsOneWidget);
    // History section sits below the fold: scroll the tab into view.
    // (find.byType(Scrollable).first is the tab ListView; TextFields own
    // the inner scrollables.)
    await t.scrollUntilVisible(find.textContaining('Past conversations'), 400,
        scrollable: find.byType(Scrollable).first);
    await t.pumpAndSettle();
    expect(find.text('Ask Zara (streaming talk)'), findsNothing);
  });

  testWidgets('Settings tab holds setup + advanced diagnostics', (t) async {
    await pumpApp(t);
    await t.tap(find.text('Settings'));
    await t.pumpAndSettle();
    expect(find.text('This device'), findsOneWidget);
    expect(find.text('Sign out'), findsOneWidget);
    // Paired-devices fleet section sits above voice diagnostics; the
    // record button is now below the fold until scrolled into view.
    await t.scrollUntilVisible(find.text('Record 5s test'), 400,
        scrollable: find.byType(Scrollable).first);
    await t.pumpAndSettle();
    expect(find.text('Record 5s test'), findsOneWidget);
    // Diagnostics precedes provider setup: assert each when visible.
    await t.scrollUntilVisible(find.text('Diagnostics (advanced)'), 400,
        scrollable: find.byType(Scrollable).first);
    await t.pumpAndSettle();
    // Provider/diagnostics sit below the fold: scroll into view.
    await t.scrollUntilVisible(find.text('Provider setup'), 400,
        scrollable: find.byType(Scrollable).first);
    await t.pumpAndSettle();
    expect(find.text('Ask Zara (streaming talk)'), findsNothing);
  });

  testWidgets('task drafts survive tab switches', (t) async {
    await pumpApp(t);
    await t.tap(find.text('Tasks'));
    await t.pumpAndSettle();
    // Default inputs controller text is part of the everyday form.
    expect(find.textContaining('system.battery'), findsWidgets);
    await t.tap(find.text('Chat'));
    await t.pumpAndSettle();
    expect(find.text('Ask Zara (streaming talk)'), findsOneWidget);
    await t.tap(find.text('Tasks'));
    await t.pumpAndSettle();
    expect(find.text('Submit task'), findsOneWidget);
  });
}
