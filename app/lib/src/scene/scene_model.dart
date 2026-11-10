import 'dart:math' as math;
import 'dart:ui';

import '../content/models.dart';

enum Face { front, back }

/// Where one view instance sits on the desk. Position is the top-left corner in
/// desk logical pixels; turn is an in-plane rotation in radians.
class Pose {
  const Pose(this.position, {this.turn = 0, this.face = Face.front});

  final Offset position;
  final double turn;
  final Face face;

  Pose copyWith({Offset? position, double? turn, Face? face}) =>
      Pose(position ?? this.position, turn: turn ?? this.turn, face: face ?? this.face);

  Map<String, Object> toJson() => {'x': position.dx, 'y': position.dy, 't': turn, 'f': face.name};

  static Pose fromJson(Map<String, Object?> json) => Pose(
        Offset((json['x'] as num).toDouble(), (json['y'] as num).toDouble()),
        turn: (json['t'] as num?)?.toDouble() ?? 0,
        face: Face.values.byName(json['f'] as String? ?? 'front'),
      );
}

/// A view of one business object. Several instances may point at the same ref;
/// none of them owns a copy of the content.
class SpatialInstance {
  const SpatialInstance({
    required this.id,
    required this.ref,
    required this.pose,
    required this.size,
    this.pinned = false,
    this.stowed = false,
  });

  final String id;
  final ObjectRef ref;
  final Pose pose;
  final Size size;
  final bool pinned;
  final bool stowed;

  SpatialInstance copyWith({Pose? pose, Size? size, bool? pinned, bool? stowed}) => SpatialInstance(
        id: id,
        ref: ref,
        pose: pose ?? this.pose,
        size: size ?? this.size,
        pinned: pinned ?? this.pinned,
        stowed: stowed ?? this.stowed,
      );

  Rect get rect => pose.position & size;

  Map<String, Object> toJson() => {
        'id': id,
        'ref': ref.key,
        'pose': pose.toJson(),
        'w': size.width,
        'h': size.height,
        'pin': pinned,
        'stow': stowed,
      };

  static SpatialInstance fromJson(Map<String, Object?> json) => SpatialInstance(
        id: json['id'] as String,
        ref: ObjectRef.parse(json['ref'] as String),
        pose: Pose.fromJson((json['pose'] as Map).cast<String, Object?>()),
        size: Size((json['w'] as num).toDouble(), (json['h'] as num).toDouble()),
        pinned: json['pin'] as bool? ?? false,
        stowed: json['stow'] as bool? ?? false,
      );
}

/// The one active grab. Holding the pre-grab pose is what makes cancel exact.
class Grab {
  const Grab(this.instanceId, this.before);
  final String instanceId;
  final Pose before;
}

class SceneState {
  const SceneState({required this.instances, required this.order, this.revision = 0, this.grab, this.selected});

  final Map<String, SpatialInstance> instances;

  /// Back to front. The last id is drawn on top.
  final List<String> order;

  /// Bumped once per committed layout change; drags in flight do not count.
  final int revision;
  final Grab? grab;
  final String? selected;

  factory SceneState.of(List<SpatialInstance> items) =>
      SceneState(instances: {for (final i in items) i.id: i}, order: [for (final i in items) i.id]);

  Iterable<SpatialInstance> get onDesk => order.map((id) => instances[id]!).where((i) => !i.stowed);
  Iterable<SpatialInstance> get stowed => order.map((id) => instances[id]!).where((i) => i.stowed);

  SceneState _with({Map<String, SpatialInstance>? instances, List<String>? order, int? revision, Grab? grab, bool clearGrab = false, String? selected, bool clearSelected = false}) =>
      SceneState(
        instances: instances ?? this.instances,
        order: order ?? this.order,
        revision: revision ?? this.revision,
        grab: clearGrab ? null : (grab ?? this.grab),
        selected: clearSelected ? null : (selected ?? this.selected),
      );

  Map<String, Object> toJson() => {'rev': revision, 'order': order, 'items': [for (final id in order) instances[id]!.toJson()]};

  static SceneState fromJson(Map<String, Object?> json) {
    final items = (json['items'] as List).map((e) => SpatialInstance.fromJson((e as Map).cast<String, Object?>())).toList();
    return SceneState(
      instances: {for (final i in items) i.id: i},
      order: (json['order'] as List).cast<String>(),
      revision: (json['rev'] as num?)?.toInt() ?? 0,
    );
  }
}

sealed class SceneCommand {
  const SceneCommand();
}

class Select extends SceneCommand {
  const Select(this.id);
  final String? id;
}

class StartGrab extends SceneCommand {
  const StartGrab(this.id);
  final String id;
}

/// Follows the pointer. Bounds keep the grip reachable inside the desk.
class DragGrab extends SceneCommand {
  const DragGrab(this.delta, this.bounds);
  final Offset delta;
  final Size bounds;
}

class ReleaseGrab extends SceneCommand {
  const ReleaseGrab();
}

class CancelGrab extends SceneCommand {
  const CancelGrab();
}

class MoveTo extends SceneCommand {
  const MoveTo(this.id, this.position, this.bounds);
  final String id;
  final Offset position;
  final Size bounds;
}

class Nudge extends SceneCommand {
  const Nudge(this.id, this.delta, this.bounds);
  final String id;
  final Offset delta;
  final Size bounds;
}

class Rotate extends SceneCommand {
  const Rotate(this.id, this.delta);
  final String id;
  final double delta;
}

class Flip extends SceneCommand {
  const Flip(this.id);
  final String id;
}

class Stow extends SceneCommand {
  const Stow(this.id);
  final String id;
}

class Recall extends SceneCommand {
  const Recall(this.id, this.bounds);
  final String id;
  final Size bounds;
}

class BringToFront extends SceneCommand {
  const BringToFront(this.id);
  final String id;
}

class TogglePin extends SceneCommand {
  const TogglePin(this.id);
  final String id;
}

class Straighten extends SceneCommand {
  const Straighten(this.id);
  final String id;
}

/// Lays unpinned desk objects out in reading order. Pinned ones stay put.
class Arrange extends SceneCommand {
  const Arrange(this.bounds);
  final Size bounds;
}

const _grabbedReach = 48.0;

Offset _clamp(Offset p, Size size, Size bounds) {
  final maxX = math.max(0.0, bounds.width - _grabbedReach);
  final maxY = math.max(0.0, bounds.height - 40);
  return Offset(p.dx.clamp(-size.width + _grabbedReach, maxX), p.dy.clamp(0.0, maxY));
}

SceneState reduceScene(SceneState s, SceneCommand c) {
  SpatialInstance? item(String id) => s.instances[id];
  SceneState put(SpatialInstance next, {bool commit = true, List<String>? order}) => s._with(
        instances: {...s.instances, next.id: next},
        order: order,
        revision: commit ? s.revision + 1 : s.revision,
      );
  List<String> raised(String id) => [...s.order.where((o) => o != id), id];

  switch (c) {
    case Select(:final id):
      return id == null ? s._with(clearSelected: true) : s._with(selected: id);
    case StartGrab(:final id):
      final target = item(id);
      if (target == null || target.pinned || target.stowed || s.grab != null) return s;
      return s._with(grab: Grab(id, target.pose), order: raised(id), selected: id);
    case DragGrab(:final delta, :final bounds):
      final grab = s.grab;
      if (grab == null) return s;
      final target = item(grab.instanceId)!;
      return put(target.copyWith(pose: target.pose.copyWith(position: _clamp(target.pose.position + delta, target.size, bounds))), commit: false);
    case ReleaseGrab():
      if (s.grab == null) return s;
      return s._with(revision: s.revision + 1, clearGrab: true);
    case CancelGrab():
      final grab = s.grab;
      if (grab == null) return s;
      final target = item(grab.instanceId)!;
      return SceneState(
        instances: {...s.instances, target.id: target.copyWith(pose: grab.before)},
        order: s.order,
        revision: s.revision,
        selected: s.selected,
      );
    case MoveTo(:final id, :final position, :final bounds):
      final target = item(id);
      if (target == null || target.pinned) return s;
      return put(target.copyWith(pose: target.pose.copyWith(position: _clamp(position, target.size, bounds))));
    case Nudge(:final id, :final delta, :final bounds):
      final target = item(id);
      if (target == null || target.pinned || target.stowed) return s;
      return put(target.copyWith(pose: target.pose.copyWith(position: _clamp(target.pose.position + delta, target.size, bounds))));
    case Rotate(:final id, :final delta):
      final target = item(id);
      if (target == null || target.pinned) return s;
      final turn = (target.pose.turn + delta).clamp(-math.pi / 6, math.pi / 6);
      return put(target.copyWith(pose: target.pose.copyWith(turn: turn)));
    case Straighten(:final id):
      final target = item(id);
      if (target == null) return s;
      return put(target.copyWith(pose: target.pose.copyWith(turn: 0)));
    case Flip(:final id):
      final target = item(id);
      if (target == null) return s;
      final face = target.pose.face == Face.front ? Face.back : Face.front;
      return put(target.copyWith(pose: target.pose.copyWith(face: face)), order: raised(id));
    case Stow(:final id):
      final target = item(id);
      if (target == null || target.stowed) return s;
      final next = put(target.copyWith(stowed: true));
      return next.selected == id ? next._with(clearSelected: true) : next;
    case Recall(:final id, :final bounds):
      final target = item(id);
      if (target == null) return s;
      final free = _freeSpot(s, target.size, bounds);
      return put(target.copyWith(stowed: false, pose: target.pose.copyWith(position: free)), order: raised(id))._with(selected: id);
    case BringToFront(:final id):
      if (item(id) == null) return s;
      return s._with(order: raised(id), selected: id, revision: s.revision + 1);
    case TogglePin(:final id):
      final target = item(id);
      if (target == null) return s;
      return put(target.copyWith(pinned: !target.pinned));
    case Arrange(:final bounds):
      var next = s;
      var x = 16.0, y = 16.0, rowHeight = 0.0;
      final updated = <String, SpatialInstance>{...s.instances};
      for (final i in s.onDesk) {
        if (i.pinned) continue;
        if (x + i.size.width > bounds.width - 16 && x > 16) {
          x = 16;
          y += rowHeight + 16;
          rowHeight = 0;
        }
        updated[i.id] = i.copyWith(pose: i.pose.copyWith(position: Offset(x, y), turn: 0));
        x += i.size.width + 16;
        rowHeight = math.max(rowHeight, i.size.height);
      }
      next = s._with(instances: updated, revision: s.revision + 1);
      return next;
  }
}

/// First top-left corner on a coarse grid that does not overlap a desk object.
Offset _freeSpot(SceneState s, Size size, Size bounds) {
  const step = 24.0;
  final taken = s.onDesk.map((i) => i.rect.inflate(8)).toList();
  for (var y = 16.0; y + size.height <= bounds.height; y += step) {
    for (var x = 16.0; x + size.width <= bounds.width; x += step) {
      final rect = Offset(x, y) & size;
      if (!taken.any(rect.overlaps)) return Offset(x, y);
    }
  }
  return const Offset(24, 24);
}
