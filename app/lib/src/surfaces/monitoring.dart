import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 监测: what is watched, what changed, and whether every source was actually
/// checked. A failed or stale source is shown as such, never as “no news”.
class MonitoringSurface extends StatelessWidget {
  const MonitoringSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  Widget build(BuildContext context) {
    final c = state.content;
    final sel = state.selection;
    final m = (sel?.kind == ObjectKind.monitor ? c.monitor(sel!.id) : null) ?? c.monitors.first;
    return LayoutBuilder(builder: (context, box) {
      final split = box.maxWidth >= 760;
      final list = _MonitorList(state: state, current: m);
      final detail = _MonitorDetail(state: state, monitor: m);
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SurfaceHeader(state: state, title: '监测', context: '${m.title} · ${m.scope}', actions: [if (!narrow) OButton('新建监测', icon: OIcon.plus, dense: true, onPressed: () => state.setOverlay(ShellLayer.create))]),
        Expanded(
          child: split
              ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [SizedBox(width: 272, child: list), const SizedBox(width: Space.dockGap), Expanded(child: detail)])
              : ListView(children: [SizedBox(height: 240, child: list), const SizedBox(height: Space.dockGap), SizedBox(height: 980, child: detail)]),
        ),
      ]);
    });
  }
}

Color _monitorColor(Palette p, MonitorState s) => switch (s) {
      MonitorState.watching => p.positive,
      MonitorState.checking => p.accentText,
      MonitorState.paused => p.textTertiary,
      MonitorState.failed => p.danger,
    };

Color _stanceColor(Palette p, Stance s) => switch (s) {
      Stance.positive => p.positive,
      Stance.neutral => p.textSecondary,
      Stance.negative => p.danger,
      Stance.question => p.warning,
    };

class _MonitorList extends StatelessWidget {
  const _MonitorList({required this.state, required this.current});
  final AppState state;
  final Monitor current;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Pane(
      padding: const EdgeInsets.fromLTRB(8, 12, 8, 8),
      child: ListView(children: [
        Padding(padding: const EdgeInsets.symmetric(horizontal: 8), child: SectionLabel('监测项 · ${state.content.monitors.length}')),
        for (final m in state.content.monitors)
          RowAction(
            selected: m.id == current.id,
            onPressed: () => state.select(ObjectRef(ObjectKind.monitor, m.id)),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Padding(padding: const EdgeInsets.only(top: 6), child: StatusDot(_monitorColor(p, m.state))),
              const SizedBox(width: 10),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(m.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                  Text('${m.state.label} · ${m.events.where((e) => !e.handled).length} 条未处理', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
                ]),
              ),
            ]),
          ),
      ]),
    );
  }
}

class _MonitorDetail extends StatelessWidget {
  const _MonitorDetail({required this.state, required this.monitor});
  final AppState state;
  final Monitor monitor;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final m = monitor;
    final counts = {for (final s in Stance.values) s: m.events.where((e) => e.stance == s).length};
    return Pane(
      padding: EdgeInsets.zero,
      child: LayoutBuilder(builder: (context, box) {
        final two = box.maxWidth >= 760;
        final events = Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          SectionLabel('事件 · ${m.events.length}'),
          for (final e in m.events) _EventRow(e),
          if (m.events.isEmpty) metaText(context, '已检查的来源中没有新事件。'),
        ]);
        final checks = Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const SectionLabel('来源检查'),
          for (final c in m.checks)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 5),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Padding(padding: const EdgeInsets.only(top: 3), child: LineIcon(c.result.contains('失败') || c.result.contains('过期') ? OIcon.alert : OIcon.check, size: 15, color: c.result.contains('失败') ? p.danger : (c.result.contains('过期') ? p.warning : p.positive))),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(c.source, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                    Text('${c.lastChecked} · ${c.result}', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
                  ]),
                ),
              ]),
            ),
          const SizedBox(height: 14),
          const SectionLabel('关键词'),
          Wrap(spacing: 6, runSpacing: 6, children: [for (final k in m.keywords) Tag(k)]),
          const SizedBox(height: 14),
          const SectionLabel('倾向分布'),
          for (final s in Stance.values)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 3),
              child: Row(children: [
                SizedBox(width: 40, child: Text(s.label, style: textStyle(p, size: 12.5, color: p.textSecondary, height: 1.3))),
                Expanded(
                  child: LayoutBuilder(builder: (context, b) {
                    final total = m.events.isEmpty ? 1 : m.events.length;
                    return Align(alignment: Alignment.centerLeft, child: Container(height: 8, width: (b.maxWidth * counts[s]! / total).clamp(2, b.maxWidth), decoration: BoxDecoration(color: _stanceColor(p, s).withValues(alpha: 0.75), borderRadius: BorderRadius.circular(4))));
                  }),
                ),
                SizedBox(width: 24, child: Text('${counts[s]}', textAlign: TextAlign.right, style: textStyle(p, size: 12.5, height: 1.3))),
              ]),
            ),
        ]);
        return ListView(padding: const EdgeInsets.fromLTRB(20, 16, 20, 20), children: [
          Row(children: [
            StatusDot(_monitorColor(p, m.state), size: 9),
            const SizedBox(width: 8),
            Text(m.state.label, style: textStyle(p, size: 13, weight: FontWeight.w600, color: _monitorColor(p, m.state), height: 1.3)),
            const Spacer(),
            OButton('暂停', dense: true, tone: ButtonTone.quiet, onPressed: () => showNotice(context, notConnected)),
            const SizedBox(width: 6),
            OButton('立即检查', icon: OIcon.refresh, dense: true, onPressed: () => showNotice(context, notConnected)),
          ]),
          const SizedBox(height: 6),
          Text(m.title, style: textStyle(p, size: 22, weight: FontWeight.w600, height: 1.35)),
          const SizedBox(height: 16),
          if (two)
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [Expanded(flex: 6, child: events), const SizedBox(width: 24), Expanded(flex: 4, child: checks)])
          else ...[events, const SizedBox(height: 18), checks],
        ]);
      }),
    );
  }
}

class _EventRow extends StatelessWidget {
  const _EventRow(this.e);
  final MonitorEvent e;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final tone = _stanceColor(p, e.stance);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
      decoration: BoxDecoration(
        color: e.handled ? Colors.transparent : p.surfaceRaised,
        borderRadius: BorderRadius.circular(Radii.control + 2),
        border: Border.all(color: p.line),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Tag(e.stance.label, tone: tone),
          const SizedBox(width: 8),
          Expanded(child: Text('${e.source} · ${e.time}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3))),
          if (e.handled) Tag('已处理', icon: OIcon.check, tone: p.positive) else if (e.replies > 0) Text('${e.replies} 条回复', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
        ]),
        const SizedBox(height: 6),
        Text(e.title, style: textStyle(p, size: 14.5, weight: FontWeight.w600, height: 1.4, color: e.handled ? p.textSecondary : p.text)),
        const SizedBox(height: 3),
        Text(e.excerpt, style: textStyle(p, size: 13.5, color: p.textSecondary, height: 1.5)),
      ]),
    );
  }
}
