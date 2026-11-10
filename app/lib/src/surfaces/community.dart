import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 创意社区: published work in its own format — video, gallery, podcast,
/// article, template, experience — with licence shown before reuse.
class CommunitySurface extends StatelessWidget {
  const CommunitySurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  Widget build(BuildContext context) {
    final c = state.content;
    final sel = state.selection;
    final current = sel?.kind == ObjectKind.release ? c.release(sel!.id) : null;
    return LayoutBuilder(builder: (context, box) {
      final cols = box.maxWidth >= 1200 ? 3 : (box.maxWidth >= 640 ? 2 : 1);
      final detailWide = current != null && box.maxWidth >= 1000;
      final feed = _Feed(state: state, columns: detailWide ? (cols - 1).clamp(1, 3) : cols);
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SurfaceHeader(state: state, title: '创意社区', context: '关注 · 推荐 · 模板 · 经验 — 作品按自身格式展示', actions: [if (!narrow) OButton('发布作品', icon: OIcon.plus, dense: true, onPressed: () => showNotice(context, notConnected))]),
        Expanded(
          child: current == null
              ? feed
              : detailWide
                  ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [Expanded(child: feed), const SizedBox(width: Space.dockGap), SizedBox(width: 400, child: _Detail(state: state, release: current))])
                  : _Detail(state: state, release: current),
        ),
      ]);
    });
  }
}

class _Feed extends StatelessWidget {
  const _Feed({required this.state, required this.columns});
  final AppState state;
  final int columns;

  @override
  Widget build(BuildContext context) {
    final items = state.content.releases;
    final lanes = List.generate(columns, (_) => <Release>[]);
    for (var n = 0; n < items.length; n++) {
      lanes[n % columns].add(items[n]);
    }
    return SingleChildScrollView(
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        for (var l = 0; l < columns; l++) ...[
          if (l > 0) const SizedBox(width: Space.dockGap),
          Expanded(child: Column(children: [for (final r in lanes[l]) Padding(padding: const EdgeInsets.only(bottom: Space.dockGap), child: _ReleaseCard(state: state, release: r))])),
        ],
      ]),
    );
  }
}

OIcon _releaseIcon(ReleaseKind k) => switch (k) {
      ReleaseKind.video || ReleaseKind.podcast => OIcon.create,
      ReleaseKind.gallery => OIcon.library,
      ReleaseKind.template => OIcon.apps,
      _ => OIcon.text,
    };

/// Sample releases carry no media, so the slot keeps the work's real aspect
/// ratio in a flat neutral tone instead of inventing imagery.
Color _cover(Palette p) => p.isNight ? const Color(0xFF23262B) : const Color(0xFFDADDE2);

class _ReleaseCard extends StatelessWidget {
  const _ReleaseCard({required this.state, required this.release});
  final AppState state;
  final Release release;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final r = release;
    final textual = r.kind == ReleaseKind.article || r.kind == ReleaseKind.experience;
    return Semantics(
      button: true,
      label: '${r.kind.label}：${r.title}',
      child: GestureDetector(
        onTap: () => state.select(ObjectRef(ObjectKind.release, r.id)),
        child: MouseRegion(
          cursor: SystemMouseCursors.click,
          child: Container(
            clipBehavior: Clip.antiAlias,
            decoration: BoxDecoration(
              color: p.surface,
              borderRadius: BorderRadius.circular(Radii.surface),
              border: Border.all(color: state.selection == ObjectRef(ObjectKind.release, r.id) ? p.accent : p.line, width: state.selection == ObjectRef(ObjectKind.release, r.id) ? 1.5 : 1),
              boxShadow: [BoxShadow(color: p.contactShadow, blurRadius: 4, offset: const Offset(0, 1))],
            ),
            child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              if (!textual)
                // No cover in the sample: show what the piece is (a screen, a set
                // of images, a recording) at a modest size instead of a big
                // grey placeholder.
                AspectRatio(
                  aspectRatio: 2.4,
                  child: Container(
                    color: r.kind == ReleaseKind.video ? const Color(0xFF15171A) : _cover(p),
                    child: Stack(children: [
                      Positioned.fill(child: _CoverStandIn(kind: r.kind, images: r.images, seed: r.title.hashCode)),
                      Positioned(left: 10, top: 10, child: _Chip(r.kind.label)),
                      if (r.duration != null) Positioned(right: 10, bottom: 10, child: _Chip(r.duration!)),
                      if (r.images > 0) Positioned(right: 10, bottom: 10, child: _Chip('${r.images} 张')),
                      const Positioned(right: 10, top: 10, child: _Chip('示例 · 无封面')),
                    ]),
                  ),
                ),
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  if (textual) Padding(padding: const EdgeInsets.only(bottom: 6), child: Tag(r.kind.label, tone: p.accentText)),
                  Text(r.title, style: textStyle(p, size: 15, weight: FontWeight.w600, height: 1.4)),
                  const SizedBox(height: 4),
                  Text(r.summary, maxLines: textual ? 4 : 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.5)),
                  if (textual && r.body.isNotEmpty) ...[
                    const SizedBox(height: 8),
                    Container(padding: const EdgeInsets.only(left: 10), decoration: BoxDecoration(border: Border(left: BorderSide(color: p.lineStrong, width: 2))), child: Text(r.body.first, style: textStyle(p, size: 13.5, height: 1.5))),
                  ],
                  const SizedBox(height: 10),
                  Row(children: [
                    Expanded(child: Text('${r.author} · ${r.published}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3))),
                    Text(r.license, style: textStyle(p, size: 11.5, color: p.textTertiary, height: 1.3)),
                  ]),
                ]),
              ),
            ]),
          ),
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(color: Colors.black.withValues(alpha: 0.45), borderRadius: BorderRadius.circular(5)),
        child: Text(text, style: textStyle(context.palette, size: 11, weight: FontWeight.w500, color: Colors.white, height: 1.2)),
      );
}

class _Detail extends StatelessWidget {
  const _Detail({required this.state, required this.release});
  final AppState state;
  final Release release;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final r = release;
    return Pane(
      padding: EdgeInsets.zero,
      child: ListView(padding: const EdgeInsets.fromLTRB(18, 10, 18, 18), children: [
        Row(children: [
          Tag(r.kind.label, tone: p.accentText),
          const Spacer(),
          IconAction(OIcon.close, label: '关闭详情', onPressed: () => state.select(null)),
        ]),
        const SizedBox(height: 4),
        Text(r.title, style: textStyle(p, size: 20, weight: FontWeight.w600, height: 1.4)),
        const SizedBox(height: 4),
        metaText(context, '${r.author} · ${r.published}${r.duration != null ? ' · ${r.duration}' : ''}'),
        const SizedBox(height: 12),
        if (r.kind != ReleaseKind.article && r.kind != ReleaseKind.experience)
          AspectRatio(
            aspectRatio: r.kind == ReleaseKind.podcast ? 2.4 : r.aspect,
            child: Container(
              decoration: BoxDecoration(color: _cover(p), borderRadius: BorderRadius.circular(Radii.control)),
              child: Center(child: LineIcon(_releaseIcon(r.kind), size: 32, color: p.textTertiary)),
            ),
          ),
        const SizedBox(height: 12),
        Text(r.summary, style: textStyle(p, size: TypeScale.body)),
        for (final b in r.body) Padding(padding: const EdgeInsets.only(top: 10), child: Text(b, style: textStyle(p, size: TypeScale.body))),
        const SizedBox(height: 16),
        const SectionLabel('使用许可'),
        KeyValue('许可', r.license, labelWidth: 52),
        KeyValue('作者', r.author, labelWidth: 52),
        const SizedBox(height: 12),
        Wrap(spacing: 8, runSpacing: 8, children: [
          OButton('收藏', icon: OIcon.pin, dense: true, onPressed: () => showNotice(context, notConnected)),
          if (r.kind == ReleaseKind.template) OButton('用此模板创建', icon: OIcon.plus, dense: true, tone: ButtonTone.primary, onPressed: () => state.setOverlay(ShellLayer.create)),
          OButton('转给 AI 讨论', icon: OIcon.ai, dense: true, tone: ButtonTone.quiet, onPressed: () => state.showTool(ToolTab.ai)),
        ]),
      ]),
    );
  }
}

class _CoverStandIn extends StatelessWidget {
  const _CoverStandIn({required this.kind, required this.images, required this.seed});
  final ReleaseKind kind;
  final int images;
  final int seed;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    switch (kind) {
      case ReleaseKind.video:
        return Center(
          child: Container(
            width: 44,
            height: 44,
            decoration: const BoxDecoration(color: Color(0x33FFFFFF), shape: BoxShape.circle),
            child: const Center(child: LineIcon(OIcon.play, size: 20, color: Colors.white)),
          ),
        );
      case ReleaseKind.gallery:
        final n = images.clamp(1, 4);
        return Padding(
          padding: const EdgeInsets.fromLTRB(12, 36, 12, 12),
          child: Row(children: [
            for (var i = 0; i < n; i++) ...[
              if (i > 0) const SizedBox(width: 6),
              Expanded(child: Container(decoration: BoxDecoration(color: p.surface.withValues(alpha: 0.55 - i * 0.08), borderRadius: BorderRadius.circular(8)))),
            ],
          ]),
        );
      case ReleaseKind.podcast:
        return Padding(
          padding: const EdgeInsets.fromLTRB(16, 36, 16, 36),
          child: CustomPaint(painter: _BarsPainter(p.textTertiary.withValues(alpha: 0.6), seed)),
        );
      default:
        // A template: the outline of the page it produces.
        return Padding(
          padding: const EdgeInsets.fromLTRB(16, 40, 16, 16),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (final w in const [0.5, 0.86, 0.72, 0.8])
              FractionallySizedBox(
                widthFactor: w,
                child: Container(height: w == 0.5 ? 10 : 6, margin: const EdgeInsets.only(bottom: 9), decoration: BoxDecoration(color: p.textTertiary.withValues(alpha: w == 0.5 ? 0.45 : 0.25), borderRadius: BorderRadius.circular(3))),
              ),
          ]),
        );
    }
  }
}

class _BarsPainter extends CustomPainter {
  _BarsPainter(this.color, this.seed);
  final Color color;
  final int seed;

  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = color
      ..strokeWidth = 2.4
      ..strokeCap = StrokeCap.round;
    final mid = size.height / 2;
    for (var x = 2.0, i = 0; x < size.width; x += 6, i++) {
      final h = (((i * 31 + seed) % 27) / 27 * 0.8 + 0.15) * mid;
      canvas.drawLine(Offset(x, mid - h), Offset(x, mid + h), paint);
    }
  }

  @override
  bool shouldRepaint(_BarsPainter old) => old.color != color || old.seed != seed;
}
