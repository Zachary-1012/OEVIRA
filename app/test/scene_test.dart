import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:oevira/src/content/models.dart';
import 'package:oevira/src/scene/scene_model.dart';

const bounds = Size(1000, 700);

SceneState desk() => SceneState.of(const [
      SpatialInstance(id: 'a', ref: ObjectRef(ObjectKind.material, 'm-interview'), pose: Pose(Offset(20, 20)), size: Size(200, 240)),
      SpatialInstance(id: 'b', ref: ObjectRef(ObjectKind.material, 'm-traffic'), pose: Pose(Offset(300, 20)), size: Size(200, 240)),
    ]);

void main() {
  test('flip turns the same instance over and brings it to the top', () {
    final s = reduceScene(desk(), const Flip('a'));
    expect(s.instances['a']!.pose.face, Face.back);
    expect(s.order.last, 'a');
    expect(reduceScene(s, const Flip('a')).instances['a']!.pose.face, Face.front);
  });

  test('drag frames do not commit; release commits once', () {
    var s = reduceScene(desk(), const StartGrab('a'));
    s = reduceScene(s, const DragGrab(Offset(30, 10), bounds));
    s = reduceScene(s, const DragGrab(Offset(30, 10), bounds));
    expect(s.revision, 0);
    expect(s.instances['a']!.pose.position, const Offset(80, 40));
    s = reduceScene(s, const ReleaseGrab());
    expect(s.revision, 1);
    expect(s.grab, isNull);
  });

  test('cancel restores the exact pose from before the grab', () {
    var s = reduceScene(desk(), const StartGrab('b'));
    s = reduceScene(s, const DragGrab(Offset(-120, 200), bounds));
    s = reduceScene(s, const CancelGrab());
    expect(s.instances['b']!.pose.position, const Offset(300, 20));
    expect(s.grab, isNull);
  });

  test('pinned objects cannot be picked up', () {
    var s = reduceScene(desk(), const TogglePin('a'));
    s = reduceScene(s, const StartGrab('a'));
    expect(s.grab, isNull);
  });

  test('stow then recall returns the object to a free spot on the desk', () {
    var s = reduceScene(desk(), const Stow('a'));
    expect(s.stowed.map((i) => i.id), ['a']);
    s = reduceScene(s, const Recall('a', bounds));
    final a = s.instances['a']!;
    expect(a.stowed, isFalse);
    expect(a.rect.overlaps(s.instances['b']!.rect), isFalse);
  });

  test('turn is limited to thirty degrees and straighten resets it', () {
    var s = desk();
    for (var n = 0; n < 20; n++) {
      s = reduceScene(s, const Rotate('a', 0.1));
    }
    expect(s.instances['a']!.pose.turn, closeTo(0.5236, 0.001));
    s = reduceScene(s, const Straighten('a'));
    expect(s.instances['a']!.pose.turn, 0);
  });

  test('layout survives a JSON round trip', () {
    final s = reduceScene(reduceScene(desk(), const Flip('b')), const Stow('a'));
    final back = SceneState.fromJson(s.toJson().cast<String, Object?>());
    expect(back.instances['b']!.pose.face, Face.back);
    expect(back.instances['a']!.stowed, isTrue);
    expect(back.order, s.order);
  });
}
