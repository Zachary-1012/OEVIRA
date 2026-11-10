import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 客户与工作: who I work for, what was commissioned, and one work in full.
class ClientsSurface extends StatelessWidget {
  const ClientsSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  Work _current() {
    final sel = state.selection;
    final c = state.content;
    if (sel?.kind == ObjectKind.work) return c.work(sel!.id) ?? c.works.first;
    if (sel?.kind == ObjectKind.client) {
      final client = c.client(sel!.id);
      final id = client?.commissions.expand((x) => x.workIds).firstOrNull;
      if (id != null) return c.work(id)!;
    }
    return c.works.firstWhere((w) => w.clientId != null, orElse: () => c.works.first);
  }

  @override
  Widget build(BuildContext context) {
    final work = _current();
    final client = work.clientId == null ? null : state.content.client(work.clientId!);
    return LayoutBuilder(builder: (context, c) {
      final split = c.maxWidth >= 760;
      final list = _ClientList(state: state, current: work);
      final detail = _WorkDetail(state: state, work: work);
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SurfaceHeader(
          state: state,
          title: '客户与工作',
          context: client == null ? '独立工作 · ${work.title}' : '${client.name} · ${client.contact}',
          boundary: client?.boundary,
          actions: [if (!narrow) OButton('新建委托', icon: OIcon.plus, dense: true, onPressed: () => state.setOverlay(ShellLayer.create))],
        ),
        Expanded(
          child: split
              ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [SizedBox(width: c.maxWidth >= 1100 ? 320 : 272, child: list), const SizedBox(width: Space.dockGap), Expanded(child: detail)])
              : ListView(children: [SizedBox(height: 300, child: list), const SizedBox(height: Space.dockGap), SizedBox(height: 900, child: detail)]),
        ),
      ]);
    });
  }
}

class _ClientList extends StatelessWidget {
  const _ClientList({required this.state, required this.current});
  final AppState state;
  final Work current;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final c = state.content;
    final independent = c.works.where((w) => w.clientId == null).toList();
    Widget workRow(Work w, {String? extra}) => RowAction(
          selected: w.id == current.id,
          onPressed: () => state.select(ObjectRef(ObjectKind.work, w.id)),
          child: Row(children: [
            StatusDot(workStateColor(p, w.state)),
            const SizedBox(width: 10),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(w.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                Text(extra ?? '${w.state.label} · ${w.updated}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
              ]),
            ),
          ]),
        );
    return Pane(
      padding: const EdgeInsets.fromLTRB(8, 12, 8, 8),
      child: ListView(children: [
        for (final client in c.clients) ...[
          Padding(
            padding: const EdgeInsets.fromLTRB(8, 4, 8, 4),
            child: Row(children: [
              LineIcon(OIcon.clients, size: 16, color: p.textSecondary),
              const SizedBox(width: 8),
              Expanded(child: Text(client.name, style: textStyle(p, size: 14, weight: FontWeight.w600, height: 1.35))),
              Text('${client.commissions.length} 项委托', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
            ]),
          ),
          for (final cm in client.commissions)
            for (final id in cm.workIds)
              if (c.work(id) case final w?) workRow(w, extra: '${cm.state} · 截止 ${cm.due}'),
          const SizedBox(height: 10),
        ],
        Padding(padding: const EdgeInsets.symmetric(horizontal: 8), child: SectionLabel('独立工作 · ${independent.length}')),
        for (final w in independent) workRow(w),
      ]),
    );
  }
}

class _WorkDetail extends StatelessWidget {
  const _WorkDetail({required this.state, required this.work});
  final AppState state;
  final Work work;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final w = work;
    final c = state.content;
    final artifacts = w.artifactIds.map(c.artifact).whereType<Artifact>().toList();
    final materials = w.materialIds.map(c.material).whereType<SourceMaterial>().toList();
    final commission = c.clients.expand((x) => x.commissions).where((cm) => cm.workIds.contains(w.id)).firstOrNull;
    return Pane(
      padding: EdgeInsets.zero,
      child: LayoutBuilder(builder: (context, box) {
        final two = box.maxWidth >= 720;
        final left = <Widget>[
          const SectionLabel('目标'),
          Text(w.goal, style: textStyle(p, size: TypeScale.body)),
          const SizedBox(height: 16),
          const SectionLabel('已确认的条件'),
          for (final k in w.conditions) _ConditionRow(k),
          if (w.conditions.isEmpty) metaText(context, '还没有确认条件。可以在对话里说明，确认后会出现在这里。'),
          if (commission != null) ...[
            const SizedBox(height: 16),
            const SectionLabel('委托'),
            KeyValue('名称', commission.title, labelWidth: 64),
            KeyValue('范围', commission.scope, labelWidth: 64),
            KeyValue('截止', commission.due, labelWidth: 64),
          ],
        ];
        final right = <Widget>[
          SectionLabel('成果 · ${artifacts.length}'),
          for (final a in artifacts)
            RowAction(
              onPressed: () => state.open(ObjectRef(ObjectKind.artifact, a.id)),
              child: Row(children: [
                LineIcon(iconForArtifact(a.kind), size: 16, color: p.textSecondary),
                const SizedBox(width: 8),
                Expanded(child: Text(a.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, height: 1.4))),
                Tag('${a.kind.label} · v${a.version}'),
              ]),
            ),
          const SizedBox(height: 14),
          SectionLabel('资料 · ${materials.length}'),
          for (final m in materials)
            RowAction(
              onPressed: () => state.open(ObjectRef(ObjectKind.material, m.id)),
              child: Row(children: [
                LineIcon(OIcon.research, size: 16, color: p.textSecondary),
                const SizedBox(width: 8),
                Expanded(child: Text(m.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, height: 1.4))),
                Text(m.kind.label, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
              ]),
            ),
          const SizedBox(height: 14),
          const SectionLabel('经过'),
          for (final t in w.timeline) _TimelineRow(t),
        ];
        return ListView(padding: const EdgeInsets.fromLTRB(20, 16, 20, 20), children: [
          Row(children: [
            StatusDot(workStateColor(p, w.state), size: 9),
            const SizedBox(width: 8),
            Text(w.state.label, style: textStyle(p, size: 13, weight: FontWeight.w600, color: workStateColor(p, w.state), height: 1.3)),
            const SizedBox(width: 10),
            Expanded(child: metaText(context, '更新于 ${w.updated}', maxLines: 1)),
            OButton('在对话中继续', icon: OIcon.ai, dense: true, tone: ButtonTone.primary, onPressed: () {
              state.select(ObjectRef(ObjectKind.work, w.id));
              state.showTool(ToolTab.ai);
            }),
          ]),
          const SizedBox(height: 8),
          Text(w.title, style: textStyle(p, size: 22, weight: FontWeight.w600, height: 1.35)),
          const SizedBox(height: 10),
          Container(
            padding: const EdgeInsets.all(12),
            decoration: BoxDecoration(color: p.accentSoft, borderRadius: BorderRadius.circular(Radii.control)),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              LineIcon(OIcon.chevron, size: 16, color: p.accentText),
              const SizedBox(width: 6),
              Expanded(child: Text('下一步：${w.nextStep}', style: textStyle(p, size: TypeScale.dense, weight: FontWeight.w500))),
            ]),
          ),
          const SizedBox(height: 18),
          if (two)
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(flex: 6, child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: left)),
              const SizedBox(width: 24),
              Expanded(flex: 5, child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: right)),
            ])
          else ...[...left, const SizedBox(height: 16), ...right],
        ]);
      }),
    );
  }
}

class _ConditionRow extends StatelessWidget {
  const _ConditionRow(this.k);
  final Condition k;
  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 5),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Padding(padding: const EdgeInsets.only(top: 3), child: LineIcon(k.confirmed ? OIcon.check : OIcon.alert, size: 15, color: k.confirmed ? p.positive : p.warning)),
        const SizedBox(width: 8),
        SizedBox(width: 72, child: Text(k.label, style: textStyle(p, size: TypeScale.dense, color: p.textSecondary))),
        Expanded(child: Text(k.confirmed ? k.value : '${k.value}（待确认）', style: textStyle(p, size: TypeScale.dense, color: k.confirmed ? p.text : p.warning))),
      ]),
    );
  }
}

class _TimelineRow extends StatelessWidget {
  const _TimelineRow(this.t);
  final TimelineEntry t;
  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(width: 86, child: Text(t.time, style: textStyle(p, size: 12.5, color: p.textTertiary, height: 1.45))),
        Expanded(child: Text('${t.actor == '我' ? '' : '${t.actor} · '}${t.text}', style: textStyle(p, size: 13.5, height: 1.45))),
      ]),
    );
  }
}
