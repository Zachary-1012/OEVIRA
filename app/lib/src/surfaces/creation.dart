import 'package:flutter/material.dart' hide Material;
import 'package:flutter/scheduler.dart';
import 'package:flutter/services.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 创作: one place for every output type. Each artifact opens in the editor
/// that suits its medium; the timeline is an instrument, not a picture of one.
class CreationSurface extends StatelessWidget {
  const CreationSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  Widget build(BuildContext context) {
    final c = state.content;
    final sel = state.selection;
    final a = (sel?.kind == ObjectKind.artifact ? c.artifact(sel!.id) : null) ?? c.artifact('a-video')!;
    final work = a.workId == null ? null : c.work(a.workId!);
    return LayoutBuilder(builder: (context, box) {
      final split = box.maxWidth >= 820;
      final rail = _ArtifactRail(state: state, current: a);
      final editor = _editorFor(a);
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SurfaceHeader(
          state: state,
          title: a.title,
          context: '创作 · ${a.kind.label} · 第 ${a.version} 版 · ${a.status}${work != null ? ' · ${work.title}' : ''}',
          actions: [
            if (!narrow) ...[
              OButton('版本', icon: OIcon.history, dense: true, tone: ButtonTone.quiet, onPressed: () => state.showTool(ToolTab.inspector)),
              OButton('导出', icon: OIcon.open, dense: true, onPressed: () => showNotice(context, notConnected)),
            ],
          ],
        ),
        Expanded(
          child: split
              ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [SizedBox(width: 232, child: rail), const SizedBox(width: Space.dockGap), Expanded(child: editor)])
              : Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [SizedBox(height: 64, child: _ArtifactStrip(state: state, current: a)), const SizedBox(height: 8), Expanded(child: editor)]),
        ),
      ]);
    });
  }

  Widget _editorFor(Artifact a) => switch (a.kind) {
        ArtifactKind.video || ArtifactKind.podcast => _MediaEditor(key: ValueKey(a.id), state: state, artifact: a),
        ArtifactKind.ppt => _SlidesEditor(key: ValueKey(a.id), artifact: a),
        ArtifactKind.sheet => _SheetEditor(state: state, artifact: a),
        ArtifactKind.html => _DashboardEditor(state: state, artifact: a),
        _ => _DocumentEditor(artifact: a),
      };
}

const _creatable = [ArtifactKind.article, ArtifactKind.word, ArtifactKind.ppt, ArtifactKind.sheet, ArtifactKind.pdf, ArtifactKind.html, ArtifactKind.video, ArtifactKind.podcast];

class _ArtifactRail extends StatelessWidget {
  const _ArtifactRail({required this.state, required this.current});
  final AppState state;
  final Artifact current;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Pane(
      padding: const EdgeInsets.fromLTRB(8, 12, 8, 8),
      child: ListView(children: [
        const Padding(padding: EdgeInsets.symmetric(horizontal: 8), child: SectionLabel('新建')),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 6),
          child: Wrap(spacing: 6, runSpacing: 6, children: [
            for (final k in _creatable) OButton(k.label, dense: true, tone: ButtonTone.quiet, onPressed: () => state.setOverlay(ShellLayer.create)),
          ]),
        ),
        const SizedBox(height: 14),
        Padding(padding: const EdgeInsets.symmetric(horizontal: 8), child: SectionLabel('正在创作 · ${state.content.artifacts.length}')),
        for (final a in state.content.artifacts)
          RowAction(
            selected: a.id == current.id,
            onPressed: () => state.select(ObjectRef(ObjectKind.artifact, a.id)),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Padding(padding: const EdgeInsets.only(top: 2), child: LineIcon(iconForArtifact(a.kind), size: 16, color: p.textSecondary)),
              const SizedBox(width: 8),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(a.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                  Text('${a.kind.label} · v${a.version} · ${a.updated}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
                ]),
              ),
            ]),
          ),
      ]),
    );
  }
}

class _ArtifactStrip extends StatelessWidget {
  const _ArtifactStrip({required this.state, required this.current});
  final AppState state;
  final Artifact current;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final items = state.content.artifacts;
    return ListView.separated(
      scrollDirection: Axis.horizontal,
      itemCount: items.length,
      separatorBuilder: (_, _) => const SizedBox(width: 8),
      itemBuilder: (context, n) => SizedBox(
        width: 180,
        child: RowAction(
          selected: items[n].id == current.id,
          onPressed: () => state.select(ObjectRef(ObjectKind.artifact, items[n].id)),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisAlignment: MainAxisAlignment.center, children: [
            Text(items[n].title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, weight: FontWeight.w500, height: 1.3)),
            Text(items[n].kind.label, style: textStyle(p, size: 11.5, color: p.textTertiary, height: 1.3)),
          ]),
        ),
      ),
    );
  }
}

String _tc(double s, {int? fps}) {
  final m = s ~/ 60, sec = (s % 60).floor();
  final base = '${m.toString().padLeft(2, '0')}:${sec.toString().padLeft(2, '0')}';
  if (fps == null) return base;
  final f = ((s - s.floor()) * fps).floor();
  return '$base:${f.toString().padLeft(2, '0')}';
}

class _MediaEditor extends StatefulWidget {
  const _MediaEditor({super.key, required this.state, required this.artifact});
  final AppState state;
  final Artifact artifact;

  @override
  State<_MediaEditor> createState() => _MediaEditorState();
}

class _MediaEditorState extends State<_MediaEditor> with SingleTickerProviderStateMixin {
  double _head = 18.4;
  bool _playing = false;
  Duration _last = Duration.zero;
  late final Ticker _ticker = createTicker((t) {
    final dt = (t - _last).inMicroseconds / 1e6;
    _last = t;
    final project = widget.state.content.mediaProject(widget.artifact.id)!;
    setState(() {
      _head += dt;
      if (_head >= project.duration) {
        _head = project.duration;
        _toggle();
      }
    });
  });
  final FocusNode _focus = FocusNode(debugLabel: 'timeline');

  void _toggle() {
    if (_playing) {
      _ticker.stop();
    } else {
      _last = Duration.zero;
      _ticker.start();
    }
    _playing = !_playing;
  }

  @override
  void dispose() {
    _ticker.dispose();
    _focus.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final project = widget.state.content.mediaProject(widget.artifact.id)!;
    final isVideo = widget.artifact.kind == ArtifactKind.video;
    final step = isVideo ? 1 / project.fps : 1.0;
    void seek(double s) => setState(() => _head = s.clamp(0, project.duration));
    final current = project.tracks.firstWhere((t) => t.kind == 'video' || t.kind == 'voice', orElse: () => project.tracks.first).clips.where((c) => c.start <= _head && _head < c.end).firstOrNull;
    return Pane(
      padding: const EdgeInsets.all(12),
      child: LayoutBuilder(builder: (context, box) {
        // The viewer takes exactly the height its aspect ratio needs at the
        // available width; the timeline gets the rest. No dead band between.
        final wideEditor = box.maxWidth >= 760;
        final side = wideEditor ? 196.0 + 12 : 0.0;
        final topH = ((box.maxWidth - side) * (isVideo ? 9 / 16 : 7 / 16)).clamp(120.0, box.maxHeight * 0.55);
        return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SizedBox(
          height: topH,
          child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Expanded(
              // One screen, not a frame inside a frame: the viewer is the
              // picture area itself at the project's aspect ratio.
              child: Align(
                alignment: Alignment.topCenter,
                child: AspectRatio(
                  aspectRatio: isVideo ? 16 / 9 : 16 / 7,
                  child: Container(
                    clipBehavior: Clip.antiAlias,
                    decoration: BoxDecoration(
                      color: const Color(0xFF0B0C0E),
                      borderRadius: BorderRadius.circular(Radii.surface),
                      boxShadow: [BoxShadow(color: p.contactShadow.withValues(alpha: p.isNight ? 0.6 : 0.18), blurRadius: 14, offset: const Offset(0, 6))],
                    ),
                    child: Stack(children: [
                      if (isVideo) Positioned.fill(child: CustomPaint(painter: _SafeAreaPainter()))
                      else Center(child: _Waveform(seed: _head.floor(), color: const Color(0xFFB8BEC8))),
                      Positioned(
                        left: 18,
                        bottom: 14,
                        right: 18,
                        child: Row(children: [
                          Expanded(child: Text(current?.label ?? '—', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 15, weight: FontWeight.w600, color: Colors.white, height: 1.3))),
                          Text(_tc(_head), style: textStyle(p, size: 12.5, weight: FontWeight.w500, color: Colors.white70, height: 1.2).copyWith(fontFeatures: const [FontFeature.tabularFigures()])),
                        ]),
                      ),
                      Positioned(
                        left: 14,
                        top: 12,
                        child: Container(
                          padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                          decoration: BoxDecoration(color: const Color(0x33FFFFFF), borderRadius: BorderRadius.circular(Radii.pill)),
                          child: Text(isVideo ? '示例工程 · 无素材画面 · 16:9' : '示例工程 · 波形示意', style: textStyle(p, size: 11, color: Colors.white, height: 1.25)),
                        ),
                      ),
                    ]),
                  ),
                ),
              ),
            ),
            if (wideEditor) ...[
            const SizedBox(width: 12),
            SizedBox(
              width: 196,
              child: ListView(children: [
                SectionLabel(isVideo ? '段落' : '章节'),
                for (final m in project.markers)
                  RowAction(
                    onPressed: () {
                      final parts = m.split(' ').first.split(':').map(int.parse).toList();
                      seek(parts[0] * 60.0 + parts[1]);
                    },
                    child: Text(m, style: textStyle(p, size: 13, height: 1.4)),
                  ),
                const SizedBox(height: 10),
                const SectionLabel('工程'),
                KeyValue('时长', _tc(project.duration), labelWidth: 52),
                if (isVideo) KeyValue('帧率', '${project.fps} fps', labelWidth: 52),
                KeyValue('轨道', '${project.tracks.length} 条', labelWidth: 52),
              ]),
            ),
            ],
          ]),
        ),
        const SizedBox(height: 10),
        Row(children: [
          IconAction(OIcon.back, label: isVideo ? '后退一帧' : '后退一秒', onPressed: () => seek(_head - step)),
          IconAction(_playing ? OIcon.pause : OIcon.play, label: _playing ? '暂停' : '播放', selected: _playing, onPressed: () => setState(_toggle)),
          IconAction(OIcon.chevron, label: isVideo ? '前进一帧' : '前进一秒', onPressed: () => seek(_head + step)),
          const SizedBox(width: 8),
          Flexible(
            child: Text.rich(
              TextSpan(children: [
                TextSpan(text: _tc(_head, fps: isVideo ? project.fps : null), style: textStyle(p, size: 14, weight: FontWeight.w600, height: 1.2).copyWith(fontFeatures: const [FontFeature.tabularFigures()])),
                TextSpan(text: '  /  ${_tc(project.duration)}', style: textStyle(p, size: 13, color: p.textTertiary, height: 1.2)),
              ]),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
            ),
          ),
          if (wideEditor) ...[
            const SizedBox(width: 12),
            Expanded(child: Align(alignment: Alignment.centerRight, child: metaText(context, '点按或拖动时间尺定位 · 方向键逐${isVideo ? '帧' : '秒'} · 空格播放', maxLines: 1))),
          ] else if (MediaQuery.sizeOf(context).width >= 700) ...[
            const SizedBox(width: 12),
            // Markers move next to the transport when the editor is too
            // narrow for a side column, so they never get clipped.
            Expanded(
              child: SizedBox(
                height: 32,
                child: ListView(scrollDirection: Axis.horizontal, children: [
                  for (final m in project.markers)
                    Padding(
                      padding: const EdgeInsets.only(left: 6),
                      child: OButton(m, dense: true, onPressed: () {
                        final parts = m.split(' ').first.split(':').map(int.parse).toList();
                        seek(parts[0] * 60.0 + parts[1]);
                      }),
                    ),
                ]),
              ),
            ),
          ],
        ]),
        const SizedBox(height: 6),
        Expanded(
          flex: 4,
          child: Focus(
            focusNode: _focus,
            onKeyEvent: (node, e) {
              if (e is! KeyDownEvent && e is! KeyRepeatEvent) return KeyEventResult.ignored;
              if (e.logicalKey == LogicalKeyboardKey.arrowLeft) {
                seek(_head - step);
                return KeyEventResult.handled;
              }
              if (e.logicalKey == LogicalKeyboardKey.arrowRight) {
                seek(_head + step);
                return KeyEventResult.handled;
              }
              if (e.logicalKey == LogicalKeyboardKey.space) {
                setState(_toggle);
                return KeyEventResult.handled;
              }
              return KeyEventResult.ignored;
            },
            child: Semantics(
              label: '时间线，当前 ${_tc(_head)}',
              slider: true,
              value: _tc(_head),
              child: LayoutBuilder(builder: (context, box) {
                const labelW = 96.0;
                final w = box.maxWidth - labelW;
                double toX(double s) => labelW + s / project.duration * w;
                void fromX(double x) => seek((x - labelW) / w * project.duration);
                return GestureDetector(
                  behavior: HitTestBehavior.opaque,
                  onTapDown: (d) {
                    _focus.requestFocus();
                    if (d.localPosition.dx > labelW) fromX(d.localPosition.dx);
                  },
                  onHorizontalDragUpdate: (d) => fromX(d.localPosition.dx),
                  child: Stack(clipBehavior: Clip.none, children: [
                    Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                      SizedBox(height: 22, child: CustomPaint(painter: _RulerPainter(p, project.duration, labelW))),
                      for (final t in project.tracks)
                        SizedBox(
                          height: ((box.maxHeight - 22) / project.tracks.length - 4).clamp(34.0, 68.0) + 4,
                          child: Container(
                            margin: const EdgeInsets.only(top: 4),
                            child: Stack(children: [
                              Positioned(left: 0, top: 0, bottom: 0, width: labelW - 8, child: Align(alignment: Alignment.centerLeft, child: Text(t.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textSecondary, height: 1.2)))),
                              Positioned.fill(left: labelW, child: Container(decoration: BoxDecoration(color: p.surfaceSunken, borderRadius: BorderRadius.circular(6)))),
                              for (final c in t.clips)
                                Positioned(
                                  left: toX(c.start) + 1,
                                  width: (c.end - c.start) / project.duration * w - 2,
                                  top: 2,
                                  bottom: 2,
                                  child: Builder(builder: (context) {
                                    final active = _head >= c.start && _head < c.end;
                                    // Clips are told apart by what they carry (frames,
                                    // waveform, a title bar), not by a rainbow of fills.
                                    return Container(
                                      clipBehavior: Clip.antiAlias,
                                      decoration: BoxDecoration(
                                        color: t.kind == 'title' ? p.surface : p.surfaceRaised,
                                        borderRadius: BorderRadius.circular(6),
                                        border: Border.all(color: active ? p.accent : p.lineStrong.withValues(alpha: 0.35), width: active ? 1.5 : 1),
                                      ),
                                      child: Stack(children: [
                                        Positioned.fill(child: CustomPaint(painter: _ClipPainter(p, t.kind, c.label.hashCode))),
                                        Positioned(
                                          left: 7,
                                          top: 4,
                                          right: 4,
                                          child: Text(c.label, maxLines: 1, overflow: TextOverflow.clip, style: textStyle(p, size: 11.5, weight: FontWeight.w600, height: 1.1, color: p.text)),
                                        ),
                                      ]),
                                    );
                                  }),
                                ),
                            ]),
                          ),
                        ),
                    ]),
                    Positioned(
                      left: toX(_head) - 6,
                      top: 0,
                      bottom: 0,
                      width: 12,
                      child: IgnorePointer(child: CustomPaint(painter: _HeadPainter(p.danger))),
                    ),
                  ]),
                );
              }),
            ),
          ),
        ),
      ]);
      }),
    );
  }

}

/// Title-safe and action-safe guides on an empty viewer, so a project with
/// no footage reads as an empty picture area rather than a placeholder box.
class _SafeAreaPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final rect = Offset.zero & size;
    canvas.drawRect(rect, Paint()..shader = const RadialGradient(radius: 0.9, colors: [Color(0xFF1A1C20), Color(0xFF0B0C0E)]).createShader(rect));
    final guide = Paint()
      ..color = const Color(0x2EFFFFFF)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1;
    canvas.drawRect(rect.deflate(size.shortestSide * 0.05), guide);
    canvas.drawRect(rect.deflate(size.shortestSide * 0.1), guide..color = const Color(0x1FFFFFFF));
    final c = rect.center;
    canvas.drawLine(c - const Offset(9, 0), c + const Offset(9, 0), guide..color = const Color(0x40FFFFFF));
    canvas.drawLine(c - const Offset(0, 9), c + const Offset(0, 9), guide);
  }

  @override
  bool shouldRepaint(_SafeAreaPainter old) => false;
}

/// What a clip carries: a strip of frame cells for picture, a waveform for
/// sound, an underline bar for titles. All neutral; selection is the accent.
class _ClipPainter extends CustomPainter {
  _ClipPainter(this.p, this.kind, this.seed);
  final Palette p;
  final String kind;
  final int seed;

  @override
  void paint(Canvas canvas, Size size) {
    final ink = Paint()..color = p.textTertiary.withValues(alpha: 0.38);
    final top = 20.0;
    if (size.height <= top + 4) return;
    switch (kind) {
      case 'video':
        const cell = 30.0;
        final band = Rect.fromLTWH(0, top, size.width, size.height - top);
        canvas.drawRect(band, Paint()..color = p.lineStrong.withValues(alpha: p.isNight ? 0.22 : 0.16));
        for (var x = cell; x < size.width; x += cell) {
          canvas.drawLine(Offset(x, top), Offset(x, size.height), Paint()..color = p.surfaceRaised.withValues(alpha: 0.9)..strokeWidth = 1.5);
        }
      case 'title':
        canvas.drawRRect(RRect.fromRectAndRadius(Rect.fromLTWH(7, size.height - 9, size.width * 0.55, 3), const Radius.circular(1.5)), ink);
      default:
        final mid = top + (size.height - top) / 2;
        final amp = (size.height - top) / 2 - 2;
        final bar = Paint()
          ..color = p.textTertiary.withValues(alpha: 0.5)
          ..strokeWidth = 1.6
          ..strokeCap = StrokeCap.round;
        for (var x = 4.0, i = 0; x < size.width - 2; x += 3.5, i++) {
          final h = (((i * 37 + seed) % 29) / 29 * 0.75 + 0.2) * amp;
          canvas.drawLine(Offset(x, mid - h), Offset(x, mid + h), bar);
        }
    }
  }

  @override
  bool shouldRepaint(_ClipPainter old) => old.p != p || old.kind != kind || old.seed != seed;
}

class _RulerPainter extends CustomPainter {
  _RulerPainter(this.p, this.duration, this.left);
  final Palette p;
  final double duration;
  final double left;

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width - left;
    final step = duration > 600 ? 300.0 : 10.0;
    final line = Paint()..color = p.lineStrong;
    for (var s = 0.0; s <= duration; s += step) {
      final x = left + s / duration * w;
      canvas.drawLine(Offset(x, size.height - 6), Offset(x, size.height), line);
      final tp = TextPainter(text: TextSpan(text: _tc(s), style: textStyle(p, size: 10.5, color: p.textTertiary, height: 1)), textDirection: TextDirection.ltr)..layout();
      if (x + tp.width < size.width) tp.paint(canvas, Offset(x + 3, 2));
    }
  }

  @override
  bool shouldRepaint(_RulerPainter old) => old.p != p || old.duration != duration;
}

class _HeadPainter extends CustomPainter {
  _HeadPainter(this.color);
  final Color color;
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()..color = color;
    final cx = size.width / 2;
    canvas.drawPath(Path()..moveTo(cx - 6, 0)..lineTo(cx + 6, 0)..lineTo(cx, 8)..close(), paint);
    canvas.drawRect(Rect.fromLTWH(cx - 0.75, 0, 1.5, size.height), paint);
  }

  @override
  bool shouldRepaint(_HeadPainter old) => old.color != color;
}

class _Waveform extends StatelessWidget {
  const _Waveform({required this.seed, required this.color});
  final int seed;
  final Color color;
  @override
  Widget build(BuildContext context) => SizedBox(width: 220, height: 80, child: CustomPaint(painter: _WavePainter(seed, color)));
}

class _WavePainter extends CustomPainter {
  _WavePainter(this.seed, this.color);
  final int seed;
  final Color color;
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round;
    for (var i = 0; i < 36; i++) {
      final h = (((i * 37 + seed * 11) % 23) / 23 * 0.8 + 0.15) * size.height;
      final x = i / 35 * size.width;
      canvas.drawLine(Offset(x, (size.height - h) / 2), Offset(x, (size.height + h) / 2), paint);
    }
  }

  @override
  bool shouldRepaint(_WavePainter old) => old.seed != seed;
}

Widget _commentsColumn(BuildContext context, Artifact a) {
  final p = context.palette;
  return ListView(children: [
    SectionLabel('审阅意见 · ${a.comments.where((c) => !c.resolved).length} 条待处理'),
    if (a.comments.isEmpty) metaText(context, '还没有审阅意见。'),
    for (final c in a.comments)
      Container(
        margin: const EdgeInsets.only(bottom: 8),
        padding: const EdgeInsets.all(10),
        decoration: BoxDecoration(color: c.resolved ? Colors.transparent : p.warningSoft, borderRadius: BorderRadius.circular(Radii.control), border: Border.all(color: p.line)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(child: Text(c.author, style: textStyle(p, size: 13, weight: FontWeight.w600, height: 1.3))),
            if (c.resolved) Tag('已解决', icon: OIcon.check, tone: p.positive),
          ]),
          const SizedBox(height: 2),
          Text(c.anchor, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
          const SizedBox(height: 4),
          Text(c.text, style: textStyle(p, size: 13.5, height: 1.5, color: c.resolved ? p.textSecondary : p.text)),
        ]),
      ),
  ]);
}

class _DocumentEditor extends StatelessWidget {
  const _DocumentEditor({required this.artifact});
  final Artifact artifact;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final a = artifact;
    return LayoutBuilder(builder: (context, box) {
      final page = Container(
        color: p.space,
        child: ListView(padding: const EdgeInsets.all(20), children: [
          Center(
            child: Container(
              constraints: const BoxConstraints(maxWidth: 640),
              padding: const EdgeInsets.fromLTRB(48, 44, 48, 52),
              decoration: BoxDecoration(color: p.paper, boxShadow: [BoxShadow(color: p.contactShadow, blurRadius: 8, offset: const Offset(0, 2))]),
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(a.title, style: textStyle(p, size: 24, weight: FontWeight.w600, height: 1.35)),
                const SizedBox(height: 6),
                metaText(context, '第 ${a.version} 版 · ${a.updated} · 示例内容'),
                const SizedBox(height: 22),
                for (var n = 0; n < a.outline.length; n++) ...[
                  Text(a.outline[n], style: textStyle(p, size: 16, weight: FontWeight.w600, height: 1.5)),
                  const SizedBox(height: 6),
                  if (n < a.sections.length)
                    SelectableText(a.sections[n], style: textStyle(p, size: TypeScale.body))
                  else
                    metaText(context, '本节正文不在示例内容中'),
                  const SizedBox(height: 20),
                ],
              ]),
            ),
          ),
        ]),
      );
      return Pane(
        padding: EdgeInsets.zero,
        child: ClipRRect(
          borderRadius: BorderRadius.circular(Radii.pane),
          child: box.maxWidth >= 900
              ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [Expanded(child: page), SizedBox(width: 300, child: Padding(padding: const EdgeInsets.all(12), child: _commentsColumn(context, a)))])
              : page,
        ),
      );
    });
  }
}

class _SlidesEditor extends StatefulWidget {
  const _SlidesEditor({super.key, required this.artifact});
  final Artifact artifact;
  @override
  State<_SlidesEditor> createState() => _SlidesEditorState();
}

class _SlidesEditorState extends State<_SlidesEditor> {
  int _slide = 0;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final a = widget.artifact;
    Widget slide(int n, {bool big = false}) => AspectRatio(
          aspectRatio: 16 / 9,
          child: Container(
            padding: EdgeInsets.all(big ? 36 : 8),
            decoration: BoxDecoration(color: p.paper, borderRadius: BorderRadius.circular(big ? 4 : 3), border: Border.all(color: n == _slide && !big ? p.accent : p.objectEdge, width: n == _slide && !big ? 2 : 1)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Text('${n + 1} / ${a.outline.length}', style: textStyle(p, size: big ? 12 : 8, color: p.textTertiary, height: 1.2)),
              SizedBox(height: big ? 14 : 4),
              Text(a.outline[n], maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: big ? 30 : 10, weight: FontWeight.w600, height: 1.3)),
              SizedBox(height: big ? 18 : 4),
              if (big && n < a.sections.length)
                Expanded(child: Text(a.sections[n], maxLines: big ? 6 : 3, overflow: TextOverflow.ellipsis, style: textStyle(p, size: big ? 18 : 7, color: p.textSecondary, height: 1.55))),
            ]),
          ),
        );
    return Pane(
      padding: const EdgeInsets.all(12),
      child: LayoutBuilder(builder: (context, box) {
        final wide = box.maxWidth >= 900;
        return Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          SizedBox(
            width: 140,
            child: ListView.separated(
              itemCount: a.outline.length,
              separatorBuilder: (_, _) => const SizedBox(height: 8),
              itemBuilder: (context, n) => Semantics(button: true, selected: n == _slide, label: '第 ${n + 1} 页 ${a.outline[n]}', child: GestureDetector(onTap: () => setState(() => _slide = n), child: MouseRegion(cursor: SystemMouseCursors.click, child: slide(n)))),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(child: Container(color: p.space, padding: const EdgeInsets.all(20), child: Center(child: slide(_slide, big: true)))),
          if (wide) ...[const SizedBox(width: 12), SizedBox(width: 280, child: _commentsColumn(context, a))],
        ]);
      }),
    );
  }
}

class _SheetEditor extends StatelessWidget {
  const _SheetEditor({required this.state, required this.artifact});
  final AppState state;
  final Artifact artifact;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final insight = state.content.subject('s-brand')?.insight;
    final series = insight?.series ?? const <double?>[];
    final headers = ['周次', '14–17 时日均小票', '较上周', '备注'];
    final rows = [
      for (var n = 0; n < series.length; n++)
        [
          '第 ${n + 1} 周',
          series[n]?.toStringAsFixed(0) ?? '缺测',
          n == 0 || series[n] == null || series[n - 1] == null ? '—' : (series[n]! - series[n - 1]!).toStringAsFixed(0),
          series[n] == null ? '收银系统升级，当周未导出' : (n == 6 ? '南口扶梯封闭' : ''),
        ],
    ];
    Widget cell(String t, {bool head = false, bool warn = false, double w = 140}) => Container(
          width: w,
          height: 34,
          padding: const EdgeInsets.symmetric(horizontal: 10),
          alignment: Alignment.centerLeft,
          decoration: BoxDecoration(color: head ? p.surfaceSunken : null, border: Border(right: BorderSide(color: p.line), bottom: BorderSide(color: p.line))),
          child: Text(t, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, weight: head ? FontWeight.w600 : FontWeight.w400, color: warn ? p.warning : p.text, height: 1.2)),
        );
    return Pane(
      padding: const EdgeInsets.all(12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Wrap(spacing: 6, children: [for (var n = 0; n < artifact.outline.length; n++) n == 0 ? Tag(artifact.outline[n], tone: p.accentText) : Tag(artifact.outline[n])]),
        const SizedBox(height: 10),
        Expanded(
          child: Container(
            decoration: BoxDecoration(border: Border.all(color: p.line), color: p.paper),
            child: SingleChildScrollView(
              scrollDirection: Axis.horizontal,
              child: SingleChildScrollView(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Row(children: [for (var c = 0; c < headers.length; c++) cell(headers[c], head: true, w: c == 3 ? 260 : 140)]),
                  for (final r in rows) Row(children: [for (var c = 0; c < r.length; c++) cell(r[c], warn: r[1] == '缺测' && c > 0, w: c == 3 ? 260 : 140)]),
                ]),
              ),
            ),
          ),
        ),
        const SizedBox(height: 8),
        metaText(context, '缺测周保持空缺，不做插值。来源：工作日时段客流表（示例）。'),
      ]),
    );
  }
}

class _DashboardEditor extends StatefulWidget {
  const _DashboardEditor({required this.state, required this.artifact});
  final AppState state;
  final Artifact artifact;
  @override
  State<_DashboardEditor> createState() => _DashboardEditorState();
}

class _DashboardEditorState extends State<_DashboardEditor> {
  int _filter = 0;
  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final data = widget.artifact.data;
    final filters = data.keys.toList();
    if (filters.isEmpty) return Pane(child: metaText(context, '这份看板没有示例数据'));
    final shown = data[filters[_filter.clamp(0, filters.length - 1)]]!;
    Widget card(String label, String value, String note) => Expanded(
          child: Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(color: p.surfaceRaised, borderRadius: BorderRadius.circular(Radii.surface), border: Border.all(color: p.line)),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              metaText(context, label),
              const SizedBox(height: 4),
              Text(value, style: textStyle(p, size: 24, weight: FontWeight.w600, height: 1.2)),
              Text(note, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
            ]),
          ),
        );
    final known = shown.whereType<double>().toList();
    return Pane(
      padding: const EdgeInsets.all(14),
      child: ListView(children: [
        Row(children: [
          for (var n = 0; n < filters.length; n++) Padding(padding: const EdgeInsets.only(right: 6), child: OButton(filters[n], dense: true, tone: n == _filter ? ButtonTone.primary : ButtonTone.quiet, onPressed: () => setState(() => _filter = n))),
        ]),
        const SizedBox(height: 12),
        Row(children: [
          card('最近一周', known.isEmpty ? '—' : known.last.toStringAsFixed(0), '日均小票'),
          const SizedBox(width: 10),
          card('三月初', known.isEmpty ? '—' : known.first.toStringAsFixed(0), '日均小票'),
          const SizedBox(width: 10),
          card('缺测', '${shown.where((v) => v == null).length} 周', '不插值'),
        ]),
        const SizedBox(height: 14),
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(color: p.surfaceRaised, borderRadius: BorderRadius.circular(Radii.surface), border: Border.all(color: p.line)),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('按周走势 · ${filters[_filter]}', style: textStyle(p, size: 14, weight: FontWeight.w600, height: 1.3)),
            const SizedBox(height: 10),
            Sparkline(shown, height: 180, highlightFrom: 5),
          ]),
        ),
      ]),
    );
  }
}
