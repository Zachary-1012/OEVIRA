import 'dart:math' as math;

import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart';

import '../brand/line_icons.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'scene_model.dart';

/// Builds one face of an object. The desk supplies the shell (grip, edge,
/// shadow); the builder supplies the working content for that face.
typedef FaceBuilder = Widget Function(BuildContext context, SpatialInstance instance, Face face);

class ObjectShellInfo {
  const ObjectShellInfo(this.title, this.kindLabel, {this.icon = OIcon.reader});
  final String title;
  final String kindLabel;
  final OIcon icon;
}

/// A working surface where objects can be picked up by their grip, moved,
/// turned, flipped, pinned, stowed and recalled. Every action also has a menu
/// and keyboard path, so dragging is never the only way.
class Desk extends StatefulWidget {
  const Desk({
    super.key,
    required this.controller,
    required this.faces,
    required this.info,
    this.onOpen,
    this.onSelect,
    this.trayLabel = '收纳盒',
  });

  final SceneController controller;
  final Map<ObjectKind, FaceBuilder> faces;
  final ObjectShellInfo Function(ObjectRef ref) info;
  final void Function(SpatialInstance instance)? onOpen;
  final void Function(SpatialInstance? instance)? onSelect;
  final String trayLabel;

  @override
  State<Desk> createState() => _DeskState();
}

class _DeskState extends State<Desk> {
  Size _bounds = const Size(800, 600);
  String? _announcement;

  void _dispatch(SceneCommand c) => widget.controller.dispatch(c);

  void _announce(String text) => setState(() => _announcement = text);

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return ValueListenableBuilder<SceneState>(
      valueListenable: widget.controller,
      builder: (context, scene, _) {
        return Column(
          children: [
            Expanded(
              child: ClipRRect(
                borderRadius: BorderRadius.circular(Radii.pane),
                child: LayoutBuilder(builder: (context, constraints) {
                  _bounds = constraints.biggest;
                  return Stack(
                    clipBehavior: Clip.hardEdge,
                    children: [
                      Positioned.fill(
                        child: GestureDetector(
                          behavior: HitTestBehavior.opaque,
                          onTap: () {
                            _dispatch(const Select(null));
                            widget.onSelect?.call(null);
                          },
                          child: CustomPaint(painter: _FloorPainter(p)),
                        ),
                      ),
                      for (final instance in scene.onDesk)
                        _DeskObject(
                          key: ValueKey(instance.id),
                          instance: instance,
                          grabbed: scene.grab?.instanceId == instance.id,
                          selected: scene.selected == instance.id,
                          info: widget.info(instance.ref),
                          face: widget.faces[instance.ref.kind]!,
                          bounds: () => _bounds,
                          dispatch: _dispatch,
                          announce: _announce,
                          onOpen: widget.onOpen,
                          onSelect: (i) {
                            _dispatch(Select(i.id));
                            widget.onSelect?.call(i);
                          },
                        ),
                      if (scene.onDesk.isEmpty)
                        Center(child: metaText(context, '桌面上没有物件。从左侧索引或下方收纳盒取出。')),
                    ],
                  );
                }),
              ),
            ),
            const SizedBox(height: 8),
            _Tray(
              scene: scene,
              label: widget.trayLabel,
              info: widget.info,
              onRecall: (id) {
                _dispatch(Recall(id, _bounds));
                _announce('已取出到桌面');
              },
              onArrange: () {
                _dispatch(Arrange(_bounds));
                _announce('已整理未固定的物件');
              },
            ),
            if (_announcement != null)
              Semantics(liveRegion: true, child: SizedBox(height: 0, child: Text(_announcement!, style: const TextStyle(fontSize: 0.1)))),
          ],
        );
      },
    );
  }
}

class _FloorPainter extends CustomPainter {
  _FloorPainter(this.p);
  final Palette p;

  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    // One stable ambient fill plus a single key light from the upper left.
    canvas.drawRect(rect, Paint()..color = p.surfaceSunken);
    canvas.drawRect(
      rect,
      Paint()
        ..shader = RadialGradient(
          center: const Alignment(-0.7, -0.9),
          radius: 1.3,
          colors: [p.surface.withValues(alpha: p.isNight ? 0.22 : 0.85), p.surfaceSunken.withValues(alpha: 0)],
        ).createShader(rect),
    );
    final dot = Paint()..color = p.spaceLine.withValues(alpha: p.isNight ? 0.9 : 1);
    for (var x = 12.0; x < size.width; x += 24) {
      for (var y = 12.0; y < size.height; y += 24) {
        canvas.drawCircle(Offset(x, y), 0.9, dot);
      }
    }
  }

  @override
  bool shouldRepaint(_FloorPainter old) => old.p != p;
}

class _DeskObject extends StatefulWidget {
  const _DeskObject({
    super.key,
    required this.instance,
    required this.grabbed,
    required this.selected,
    required this.info,
    required this.face,
    required this.bounds,
    required this.dispatch,
    required this.announce,
    required this.onSelect,
    this.onOpen,
  });

  final SpatialInstance instance;
  final bool grabbed;
  final bool selected;
  final ObjectShellInfo info;
  final FaceBuilder face;
  final Size Function() bounds;
  final void Function(SceneCommand) dispatch;
  final void Function(String) announce;
  final void Function(SpatialInstance) onSelect;
  final void Function(SpatialInstance)? onOpen;

  @override
  State<_DeskObject> createState() => _DeskObjectState();
}

class _DeskObjectState extends State<_DeskObject> with TickerProviderStateMixin {
  late final AnimationController _flip = AnimationController(vsync: this, duration: Motion.long, value: widget.instance.pose.face == Face.back ? 1 : 0);
  late final Ticker _glide = createTicker(_onGlide);
  final FocusNode _focus = FocusNode(debugLabel: 'desk-object');
  VelocityTracker? _tracker;
  Offset? _downAt;
  PointerDeviceKind _device = PointerDeviceKind.mouse;
  bool _dragging = false;
  Offset _velocity = Offset.zero;
  Duration _lastGlide = Duration.zero;
  bool _keyboardCarry = false;

  bool get _reduced => MediaQuery.maybeDisableAnimationsOf(context) ?? false;

  @override
  void didUpdateWidget(_DeskObject old) {
    super.didUpdateWidget(old);
    if (old.instance.pose.face != widget.instance.pose.face) {
      final target = widget.instance.pose.face == Face.back ? 1.0 : 0.0;
      if (_reduced) {
        _flip.value = target;
      } else {
        _flip.animateTo(target, curve: Motion.standard);
      }
    }
  }

  @override
  void dispose() {
    _glide.dispose();
    _flip.dispose();
    _focus.dispose();
    super.dispose();
  }

  void _onPointerDown(PointerDownEvent e) {
    if (_glide.isActive) _endGlide();
    _downAt = e.position;
    _device = e.kind;
    _tracker = VelocityTracker.withKind(e.kind)..addPosition(e.timeStamp, e.position);
    widget.onSelect(widget.instance);
    _focus.requestFocus();
  }

  void _onPointerMove(PointerMoveEvent e) {
    final start = _downAt;
    if (start == null) return;
    _tracker?.addPosition(e.timeStamp, e.position);
    if (!_dragging) {
      final slop = _device == PointerDeviceKind.touch ? Motion.touchSlop : Motion.mouseSlop;
      if ((e.position - start).distance < slop) return;
      if (widget.instance.pinned) {
        widget.announce('已固定，先取消固定再移动');
        _downAt = null;
        return;
      }
      _dragging = true;
      widget.dispatch(StartGrab(widget.instance.id));
      widget.dispatch(DragGrab(e.position - start, widget.bounds()));
      return;
    }
    widget.dispatch(DragGrab(e.delta, widget.bounds()));
  }

  void _onPointerUp(PointerUpEvent e) {
    _downAt = null;
    if (!_dragging) return;
    _dragging = false;
    final v = _tracker?.getVelocity().pixelsPerSecond ?? Offset.zero;
    if (_reduced || v.distance < 80) {
      widget.dispatch(const ReleaseGrab());
      return;
    }
    final limit = 2400.0;
    _velocity = v.distance > limit ? v / v.distance * limit : v;
    _lastGlide = Duration.zero;
    _glide.start();
  }

  void _onPointerCancel(PointerCancelEvent e) {
    _downAt = null;
    if (_dragging) {
      _dragging = false;
      widget.dispatch(const ReleaseGrab());
    }
  }

  /// Free slide after release: v(t) = v0·exp(−λt), integrated per frame.
  void _onGlide(Duration elapsed) {
    final dt = ((elapsed - _lastGlide).inMicroseconds / 1e6).clamp(0.0, 1 / 30);
    _lastGlide = elapsed;
    final decay = math.exp(-Motion.slideLambda * dt);
    final step = _velocity * ((1 - decay) / Motion.slideLambda);
    _velocity = _velocity * decay;
    widget.dispatch(DragGrab(step, widget.bounds()));
    if (_velocity.distance < 20) _endGlide();
  }

  void _endGlide() {
    _glide.stop();
    widget.dispatch(const ReleaseGrab());
  }

  KeyEventResult _onKey(FocusNode node, KeyEvent event) {
    if (event is! KeyDownEvent && event is! KeyRepeatEvent) return KeyEventResult.ignored;
    final id = widget.instance.id;
    final big = HardwareKeyboard.instance.isShiftPressed;
    final step = big ? 32.0 : 8.0;
    final key = event.logicalKey;
    final moves = {
      LogicalKeyboardKey.arrowLeft: Offset(-step, 0),
      LogicalKeyboardKey.arrowRight: Offset(step, 0),
      LogicalKeyboardKey.arrowUp: Offset(0, -step),
      LogicalKeyboardKey.arrowDown: Offset(0, step),
    };
    if (moves.containsKey(key)) {
      if (!_keyboardCarry) {
        _keyboardCarry = true;
        widget.dispatch(StartGrab(id));
        widget.announce('已拿起，方向键移动，Enter 放下，Esc 取消');
      }
      widget.dispatch(DragGrab(moves[key]!, widget.bounds()));
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.escape && _keyboardCarry) {
      _keyboardCarry = false;
      widget.dispatch(const CancelGrab());
      widget.announce('已取消，回到拿起前的位置');
      return KeyEventResult.handled;
    }
    if (key == LogicalKeyboardKey.enter || key == LogicalKeyboardKey.numpadEnter) {
      if (_keyboardCarry) {
        _keyboardCarry = false;
        widget.dispatch(const ReleaseGrab());
        widget.announce('已放下');
      } else {
        widget.onOpen?.call(widget.instance);
      }
      return KeyEventResult.handled;
    }
    final commands = <LogicalKeyboardKey, (SceneCommand, String)>{
      LogicalKeyboardKey.keyF: (Flip(id), '已翻面'),
      LogicalKeyboardKey.keyS: (Stow(id), '已收纳'),
      LogicalKeyboardKey.keyP: (TogglePin(id), widget.instance.pinned ? '已取消固定' : '已固定'),
      LogicalKeyboardKey.bracketLeft: (Rotate(id, -math.pi / 12), '向左转 15°'),
      LogicalKeyboardKey.bracketRight: (Rotate(id, math.pi / 12), '向右转 15°'),
      LogicalKeyboardKey.digit0: (Straighten(id), '已摆正'),
    };
    final hit = commands[key];
    if (hit == null) return KeyEventResult.ignored;
    widget.dispatch(hit.$1);
    widget.announce(hit.$2);
    return KeyEventResult.handled;
  }

  Future<void> _menu(Offset globalPosition) async {
    final i = widget.instance;
    final p = context.palette;
    final overlay = Overlay.of(context).context.findRenderObject() as RenderBox;
    final items = <(String, OIcon, VoidCallback)>[
      if (widget.onOpen != null) ('正对我 · 打开', OIcon.reader, () => widget.onOpen!(i)),
      (i.pose.face == Face.front ? '翻到背面 · 来源与版本' : '翻回正面', OIcon.flip, () => widget.dispatch(Flip(i.id))),
      ('向左转 15°', OIcon.refresh, () => widget.dispatch(Rotate(i.id, -math.pi / 12))),
      ('摆正', OIcon.focus, () => widget.dispatch(Straighten(i.id))),
      ('带到最前', OIcon.front, () => widget.dispatch(BringToFront(i.id))),
      (i.pinned ? '取消固定' : '固定位置', OIcon.pin, () => widget.dispatch(TogglePin(i.id))),
      ('收纳', OIcon.stow, () => widget.dispatch(Stow(i.id))),
    ];
    final chosen = await showMenu<int>(
      context: context,
      color: p.surfaceRaised,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Radii.surface), side: BorderSide(color: p.line)),
      position: RelativeRect.fromRect(globalPosition & const Size(1, 1), Offset.zero & overlay.size),
      items: [
        for (var n = 0; n < items.length; n++)
          PopupMenuItem<int>(
            value: n,
            height: Targets.click,
            child: Row(children: [
              LineIcon(items[n].$2, size: 18, color: p.textSecondary),
              const SizedBox(width: 10),
              Text(items[n].$1, style: textStyle(p, size: TypeScale.dense)),
            ]),
          ),
      ],
    );
    if (chosen != null) items[chosen].$3();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final i = widget.instance;
    final lifted = widget.grabbed;
    final shadow = lifted
        ? [BoxShadow(color: p.liftShadow, blurRadius: 28, offset: const Offset(6, 16)), BoxShadow(color: p.contactShadow, blurRadius: 4, offset: const Offset(0, 1))]
        : [BoxShadow(color: p.contactShadow, blurRadius: 6, offset: const Offset(1, 2)), BoxShadow(color: p.contactShadow.withValues(alpha: 0.08), blurRadius: 18, offset: const Offset(3, 8))];

    Widget faceShell(Face face) => _ObjectSurface(
          info: widget.info,
          face: face,
          pinned: i.pinned,
          selected: widget.selected,
          focused: _focus.hasFocus,
          gripListener: (child) => Listener(
            onPointerDown: _onPointerDown,
            onPointerMove: _onPointerMove,
            onPointerUp: _onPointerUp,
            onPointerCancel: _onPointerCancel,
            child: MouseRegion(cursor: i.pinned ? SystemMouseCursors.basic : (lifted ? SystemMouseCursors.grabbing : SystemMouseCursors.grab), child: child),
          ),
          onMenu: _menu,
          onFlip: () => widget.dispatch(Flip(i.id)),
          child: widget.face(context, i, face),
        );

    return AnimatedPositioned(
      duration: Duration.zero,
      left: i.pose.position.dx,
      top: i.pose.position.dy,
      width: i.size.width,
      height: i.size.height,
      child: Focus(
        focusNode: _focus,
        onKeyEvent: _onKey,
        onFocusChange: (_) => setState(() {}),
        child: Semantics(
          container: true,
          label: '${widget.info.kindLabel}：${widget.info.title}，${i.pose.face == Face.front ? '正面' : '背面'}${i.pinned ? '，已固定' : ''}',
          hint: '方向键移动，F 翻面，S 收纳，P 固定，Enter 打开',
          child: GestureDetector(
            onTap: () => widget.onSelect(i),
            onSecondaryTapUp: (d) => _menu(d.globalPosition),
            onLongPressStart: (d) => _menu(d.globalPosition),
            child: Transform.rotate(
              angle: i.pose.turn,
              child: AnimatedScale(
                scale: lifted ? 1.018 : 1,
                duration: _reduced ? Duration.zero : Motion.short,
                child: AnimatedBuilder(
                  animation: _flip,
                  builder: (context, _) {
                    final angle = _flip.value * math.pi;
                    final showBack = angle > math.pi / 2;
                    // Edge-on, the panel narrows; the visible face is chosen by
                    // which side faces the viewer, and the back is counter-rotated
                    // so its text reads normally rather than mirrored.
                    final matrix = Matrix4.identity()
                      ..setEntry(3, 2, 0.0011)
                      ..rotateY(angle);
                    return Transform(
                      alignment: Alignment.center,
                      transform: matrix,
                      child: DecoratedBox(
                        decoration: BoxDecoration(borderRadius: BorderRadius.circular(Radii.object), boxShadow: shadow),
                        child: showBack
                            ? Transform(alignment: Alignment.center, transform: Matrix4.rotationY(math.pi), child: faceShell(Face.back))
                            : faceShell(Face.front),
                      ),
                    );
                  },
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _ObjectSurface extends StatelessWidget {
  const _ObjectSurface({
    required this.info,
    required this.face,
    required this.pinned,
    required this.selected,
    required this.focused,
    required this.gripListener,
    required this.onMenu,
    required this.onFlip,
    required this.child,
  });

  final ObjectShellInfo info;
  final Face face;
  final bool pinned;
  final bool selected;
  final bool focused;
  final Widget Function(Widget) gripListener;
  final void Function(Offset) onMenu;
  final VoidCallback onFlip;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final back = face == Face.back;
    return Container(
      decoration: BoxDecoration(
        color: back ? p.paperBack : p.paper,
        borderRadius: BorderRadius.circular(Radii.object),
        border: Border.all(color: focused ? p.focus : (selected ? p.accent.withValues(alpha: 0.7) : p.objectEdge), width: focused ? 2 : 1),
      ),
      clipBehavior: Clip.antiAlias,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          gripListener(
            Container(
              height: 36,
              padding: const EdgeInsets.only(left: 4, right: 2),
              decoration: BoxDecoration(
                color: back ? p.surfaceSunken : p.surfaceSunken.withValues(alpha: 0.6),
                border: Border(bottom: BorderSide(color: p.line)),
              ),
              child: Row(children: [
                Tooltip(message: pinned ? '已固定' : '按住这里拿起', child: LineIcon(pinned ? OIcon.pin : OIcon.grip, size: 16, color: p.textTertiary)),
                const SizedBox(width: 4),
                LineIcon(info.icon, size: 16, color: p.accentText),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    back ? '背面 · ${info.title}' : info.title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: textStyle(p, size: 13, weight: FontWeight.w600, height: 1.3),
                  ),
                ),
                IconAction(OIcon.flip, label: back ? '翻回正面' : '翻到背面', size: 32, glyph: 16, onPressed: onFlip),
                Builder(builder: (context) {
                  return IconAction(OIcon.more, label: '物件动作', size: 32, glyph: 16, onPressed: () {
                    final box = context.findRenderObject() as RenderBox;
                    onMenu(box.localToGlobal(box.size.bottomLeft(Offset.zero)));
                  });
                }),
              ]),
            ),
          ),
          Expanded(child: child),
        ],
      ),
    );
  }
}

class _Tray extends StatelessWidget {
  const _Tray({required this.scene, required this.label, required this.info, required this.onRecall, required this.onArrange});

  final SceneState scene;
  final String label;
  final ObjectShellInfo Function(ObjectRef) info;
  final void Function(String id) onRecall;
  final VoidCallback onArrange;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final stowed = scene.stowed.toList();
    return Container(
      height: 48,
      padding: const EdgeInsets.symmetric(horizontal: 8),
      decoration: BoxDecoration(color: p.surface, borderRadius: BorderRadius.circular(Radii.surface), border: Border.all(color: p.line)),
      child: Row(children: [
        LineIcon(OIcon.stow, size: 18, color: p.textSecondary),
        const SizedBox(width: 6),
        Text('$label · ${stowed.length}', style: textStyle(p, size: 13, weight: FontWeight.w600, color: p.textSecondary, height: 1.2)),
        const SizedBox(width: 8),
        Expanded(
          child: stowed.isEmpty
              ? metaText(context, '收纳的物件会留在这里，不会被删除', maxLines: 1)
              : ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: stowed.length,
                  separatorBuilder: (_, _) => const SizedBox(width: 6),
                  itemBuilder: (context, n) {
                    final s = stowed[n];
                    return Center(
                      child: OButton(info(s.ref).title, icon: info(s.ref).icon, dense: true, onPressed: () => onRecall(s.id)),
                    );
                  },
                ),
        ),
        OButton('整理', icon: OIcon.apps, dense: true, tone: ButtonTone.quiet, onPressed: onArrange),
        Padding(padding: const EdgeInsets.only(left: 4), child: metaText(context, '布局存在本机')),
      ]),
    );
  }
}
