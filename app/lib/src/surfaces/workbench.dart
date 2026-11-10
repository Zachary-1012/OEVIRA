import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../navigation/destinations.dart';
import '../shell/tool_panel.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 工作台: the conversation is the front door; continuing work and the few
/// items waiting on me sit beside it, never in front of it.
class WorkbenchSurface extends StatelessWidget {
  const WorkbenchSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  Widget build(BuildContext context) {
    return LayoutBuilder(builder: (context, c) {
      final side = c.maxWidth >= 760;
      final chat = Pane(padding: EdgeInsets.zero, child: AiConversation(state: state, conversationKey: 'workbench', large: true));
      final header = SurfaceHeader(state: state, title: '工作台', context: '下午好。${state.content.works.where((w) => w.state != WorkState.delivered).length} 项工作在进行，${_pending(state).length} 件事等你决定。');
      if (side) {
        return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          header,
          Expanded(
            child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              Expanded(flex: 5, child: chat),
              const SizedBox(width: Space.dockGap),
              SizedBox(width: c.maxWidth >= 1100 ? 360 : 300, child: _Side(state: state)),
            ]),
          ),
          const SizedBox(height: Space.dockGap + 4),
          _ContinueStrip(state: state),
        ]);
      }
      return ListView(children: [
        header,
        SizedBox(height: narrow ? 460 : 520, child: chat),
        const SizedBox(height: Space.dockGap),
        _ContinueStrip(state: state, all: true),
        const SizedBox(height: Space.dockGap),
        _Side(state: state, scrolls: false),
      ]);
    });
  }
}

class _PendingItem {
  const _PendingItem(this.icon, this.title, this.detail, this.ref, this.tone);
  final OIcon icon;
  final String title;
  final String detail;
  final ObjectRef ref;
  final Color Function(Palette) tone;
}

List<_PendingItem> _pending(AppState s) {
  final c = s.content;
  return [
    for (final m in c.monitors)
      for (final e in m.events.where((e) => !e.handled && e.stance != Stance.positive).take(1))
        _PendingItem(OIcon.monitor, e.title, '${m.title} · ${e.source} · ${e.time}', ObjectRef(ObjectKind.monitor, m.id), (p) => e.stance == Stance.negative ? p.danger : p.warning),
    for (final a in c.artifacts)
      if (a.comments.any((x) => !x.resolved))
        _PendingItem(OIcon.text, '${a.title} 有 ${a.comments.where((x) => !x.resolved).length} 条审阅意见', a.comments.firstWhere((x) => !x.resolved).text, ObjectRef(ObjectKind.artifact, a.id), (p) => p.accentText),
    for (final w in c.works)
      for (final k in w.conditions.where((k) => !k.confirmed).take(1))
        _PendingItem(OIcon.alert, '待确认：${k.label}', '${w.title} · ${k.value}', ObjectRef(ObjectKind.work, w.id), (p) => p.warning),
  ];
}

class _Side extends StatelessWidget {
  const _Side({required this.state, this.scrolls = true});
  final AppState state;
  final bool scrolls;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final items = _pending(state);
    final monitors = state.content.monitors;
    final children = <Widget>[
      SectionLabel('等你决定 · ${items.length}'),
      for (final it in items)
        RowAction(
          onPressed: () => state.open(it.ref),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Padding(padding: const EdgeInsets.only(top: 2), child: LineIcon(it.icon, size: 16, color: it.tone(p))),
            const SizedBox(width: 10),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(it.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                const SizedBox(height: 2),
                Text(it.detail, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
              ]),
            ),
          ]),
        ),
      const SizedBox(height: 14),
      SectionLabel('监测概况', trailing: OButton('全部', dense: true, tone: ButtonTone.quiet, onPressed: () => state.go(Destination.monitoring))),
      for (final m in monitors)
        Padding(
          padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 8),
          child: Row(children: [
            StatusDot(m.state == MonitorState.failed ? p.danger : (m.state == MonitorState.paused ? p.textTertiary : p.positive)),
            const SizedBox(width: 8),
            Expanded(child: Text(m.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, height: 1.4))),
            Text('${m.events.where((e) => !e.handled).length} 条新', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
          ]),
        ),
    ];
    return Pane(
      padding: const EdgeInsets.fromLTRB(8, 12, 8, 8),
      child: scrolls ? ListView(children: children) : Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: children),
    );
  }
}

/// Whole cards that fit the row, never a half-cut card at the edge. Anything
/// that does not fit is one link away rather than hidden off-screen.
class _ContinueStrip extends StatelessWidget {
  const _ContinueStrip({required this.state, this.all = false});
  final AppState state;
  final bool all;

  @override
  Widget build(BuildContext context) {
    final works = state.content.works;
    return LayoutBuilder(builder: (context, c) {
      const gap = Space.dockGap;
      final columns = all ? 1 : ((c.maxWidth + gap) / (240 + gap)).floor().clamp(1, 4);
      final shown = all ? works : works.take(columns).toList();
      final cardW = (c.maxWidth - gap * (columns - 1)) / columns;
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Padding(
          padding: const EdgeInsets.only(left: 4, bottom: 4),
          child: SectionLabel(
            '继续工作',
            trailing: shown.length < works.length ? OButton('全部 ${works.length} 项', dense: true, tone: ButtonTone.quiet, onPressed: () => state.go(Destination.clients)) : null,
          ),
        ),
        Wrap(spacing: gap, runSpacing: gap, children: [
          for (final w in shown) SizedBox(width: cardW, height: 136, child: WorkCard(state: state, work: w)),
        ]),
      ]);
    });
  }
}

/// Paper-like card for one work; tapping continues it where it was left.
class WorkCard extends StatelessWidget {
  const WorkCard({super.key, required this.state, required this.work});
  final AppState state;
  final Work work;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final w = work;
    return Semantics(
      button: true,
      label: '继续：${w.title}',
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(Radii.surface),
          onTap: () => state.open(ObjectRef(ObjectKind.work, w.id)),
          child: Ink(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
            decoration: BoxDecoration(
              color: p.surface,
              borderRadius: BorderRadius.circular(Radii.surface),
              border: Border.all(color: p.isNight ? p.line : p.line.withValues(alpha: 0.7)),
              boxShadow: [BoxShadow(color: p.contactShadow.withValues(alpha: p.isNight ? 0.4 : 0.07), blurRadius: 10, offset: const Offset(0, 3))],
            ),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Row(children: [
                StatusDot(workStateColor(p, w.state)),
                const SizedBox(width: 6),
                Text(w.state.label, style: textStyle(p, size: 12, weight: FontWeight.w600, color: workStateColor(p, w.state), height: 1.3)),
                const Spacer(),
                Text(w.updated, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
              ]),
              const SizedBox(height: 6),
              Text(w.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 14.5, weight: FontWeight.w600, height: 1.35)),
              const SizedBox(height: 3),
              Expanded(child: Text('下一步：${w.nextStep}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12.5, color: p.textSecondary, height: 1.45))),
              Text(w.clientId == null ? '独立工作' : '客户 · ${state.content.client(w.clientId!)?.name ?? ''}', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
            ]),
          ),
        ),
      ),
    );
  }
}
