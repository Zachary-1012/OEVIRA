import 'package:flutter/gestures.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:oevira/src/app.dart';
import 'package:oevira/src/content/sample/sample_content.dart';
import 'package:oevira/src/navigation/destinations.dart';
import 'package:oevira/src/state/app_state.dart';
import 'package:shared_preferences/shared_preferences.dart';

Future<void> pumpAt(WidgetTester tester, Size size, {Destination? d, Appearance? a}) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(OeviraApp(content: const SampleContentSource(), preferences: prefs, initialDestination: d, initialAppearance: a));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('navigation shows the three groups and none of the old four entries', (tester) async {
    await pumpAt(tester, const Size(1440, 900));
    for (final g in ['工作', '专业', '内容']) {
      expect(find.text(g), findsWidgets);
    }
    for (final d in Destination.values.where((d) => d.group != null)) {
      expect(find.text(d.label), findsWidgets);
    }
    // The old bottom bar entries 监测/资料/成果 never appear as a standalone set.
    expect(find.text('资料'), findsNothing);
    expect(find.text('成果'), findsNothing);
    expect(find.text('示例内容'), findsWidgets);
  });

  for (final d in Destination.values) {
    for (final size in const [Size(1440, 900), Size(1024, 768), Size(390, 844)]) {
      testWidgets('${d.name} renders at ${size.width.toInt()} without overflow', (tester) async {
        await pumpAt(tester, size, d: d);
        expect(tester.takeException(), isNull);
      });
    }
  }

  testWidgets('night appearance renders the research desk', (tester) async {
    await pumpAt(tester, const Size(1440, 900), d: Destination.research, a: Appearance.night);
    expect(find.text('常客访谈记录（12 位）'), findsWidgets);
    expect(tester.takeException(), isNull);
  });

  testWidgets('flipping a research material shows its provenance on the back', (tester) async {
    await pumpAt(tester, const Size(1440, 900), d: Destination.research);
    final flip = find.byTooltip('翻到背面');
    expect(flip, findsWidgets);
    await tester.tap(flip.first);
    await tester.pumpAndSettle();
    expect(find.text('版本 · 来源 · 方法 · 许可'), findsWidgets);
  });

  testWidgets('dragging a grip strip moves the same object', (tester) async {
    await pumpAt(tester, const Size(1440, 900), d: Destination.research);
    final grip = find.byTooltip('按住这里拿起').last;
    final before = tester.getTopLeft(grip);
    final gesture = await tester.startGesture(tester.getCenter(grip), kind: PointerDeviceKind.mouse);
    for (var n = 0; n < 10; n++) {
      await gesture.moveBy(const Offset(12, 8));
      await tester.pump(const Duration(milliseconds: 16));
    }
    await gesture.up();
    await tester.pumpAndSettle();
    final after = tester.getTopLeft(grip);
    expect(after.dx - before.dx, greaterThan(60));
    expect(after.dy - before.dy, greaterThan(40));
  });
}
