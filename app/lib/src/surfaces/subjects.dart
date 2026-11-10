import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 主体与经营: who a subject is, how it relates to others, and one operating
/// question answered as question → change → reason → evidence → action.
class SubjectsSurface extends StatelessWidget {
  const SubjectsSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  Widget build(BuildContext context) {
    final c = state.content;
    final sel = state.selection;
    final subject = (sel?.kind == ObjectKind.subject ? c.subject(sel!.id) : null) ?? c.subjects.first;
    return LayoutBuilder(builder: (context, box) {
      final split = box.maxWidth >= 760;
      final list = _SubjectList(state: state, current: subject);
      final detail = _SubjectDetail(state: state, subject: subject);
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SurfaceHeader(state: state, title: '主体与经营', context: '${subject.kind.label} · ${subject.name} · ${subject.city}', actions: [if (!narrow) OButton('添加主体', icon: OIcon.plus, dense: true, onPressed: () => state.setOverlay(ShellLayer.create))]),
        Expanded(
          child: split
              ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [SizedBox(width: 260, child: list), const SizedBox(width: Space.dockGap), Expanded(child: detail)])
              : ListView(children: [SizedBox(height: 260, child: list), const SizedBox(height: Space.dockGap), SizedBox(height: 1100, child: detail)]),
        ),
      ]);
    });
  }
}

class _SubjectList extends StatelessWidget {
  const _SubjectList({required this.state, required this.current});
  final AppState state;
  final Subject current;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Pane(
      padding: const EdgeInsets.fromLTRB(8, 12, 8, 8),
      child: ListView(children: [
        for (final kind in SubjectKind.values)
          if (state.content.subjects.where((s) => s.kind == kind).isNotEmpty) ...[
            Padding(padding: const EdgeInsets.symmetric(horizontal: 8), child: SectionLabel(kind.label)),
            for (final s in state.content.subjects.where((s) => s.kind == kind))
              RowAction(
                selected: s.id == current.id,
                onPressed: () => state.select(ObjectRef(ObjectKind.subject, s.id)),
                child: Row(children: [
                  Expanded(child: Text(s.name, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4))),
                  if (s.insight != null) Tag('经营问题', tone: p.accentText),
                  if (s.facts.any((f) => f.pending)) Padding(padding: const EdgeInsets.only(left: 6), child: StatusDot(p.warning)),
                ]),
              ),
            const SizedBox(height: 8),
          ],
      ]),
    );
  }
}

class _SubjectDetail extends StatelessWidget {
  const _SubjectDetail({required this.state, required this.subject});
  final AppState state;
  final Subject subject;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final s = subject;
    return Pane(
      padding: EdgeInsets.zero,
      child: LayoutBuilder(builder: (context, box) {
        final two = box.maxWidth >= 760;
        final profile = Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const SectionLabel('档案'),
          for (final f in s.facts)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 4),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                SizedBox(width: 84, child: Text(f.label, style: textStyle(p, size: 13, color: p.textTertiary, height: 1.45))),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(f.value, style: textStyle(p, size: 13.5, color: f.pending ? p.warning : p.text, height: 1.45)),
                    Text(f.pending ? '待核 · ${f.source}' : '来源 · ${f.source}', style: textStyle(p, size: 11.5, color: p.textTertiary, height: 1.35)),
                  ]),
                ),
              ]),
            ),
          const SizedBox(height: 14),
          const SectionLabel('关系'),
          for (final r in s.relations)
            RowAction(
              onPressed: () => state.select(ObjectRef(ObjectKind.subject, r.targetId)),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Padding(padding: const EdgeInsets.only(top: 2), child: LineIcon(OIcon.link, size: 15, color: p.accentText)),
                const SizedBox(width: 8),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text('${r.role} · ${state.content.subject(r.targetId)?.name ?? r.targetId}', style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                    Text('${r.period} · 证据：${r.evidence}', style: textStyle(p, size: 12, color: p.textTertiary, height: 1.35)),
                  ]),
                ),
              ]),
            ),
          if (s.relations.isEmpty) metaText(context, '暂无已核实关系'),
        ]);
        final insight = s.insight == null
            ? Container(
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(color: p.surfaceSunken, borderRadius: BorderRadius.circular(Radii.surface)),
                child: metaText(context, '这个主体还没有经营问题。在对话里提出一个问题，例如“这家店为什么午后变冷清”，寰意会按问题、变化、原因、证据、行动来整理。'),
              )
            : _InsightPanel(insight: s.insight!);
        return ListView(padding: const EdgeInsets.fromLTRB(20, 16, 20, 20), children: [
          Row(children: [Tag(s.kind.label, tone: p.accentText), const SizedBox(width: 8), metaText(context, s.city)]),
          const SizedBox(height: 6),
          Text(s.name, style: textStyle(p, size: 22, weight: FontWeight.w600, height: 1.35)),
          const SizedBox(height: 16),
          if (two)
            Row(crossAxisAlignment: CrossAxisAlignment.start, children: [Expanded(flex: 4, child: profile), const SizedBox(width: 24), Expanded(flex: 6, child: insight)])
          else ...[insight, const SizedBox(height: 18), profile],
        ]);
      }),
    );
  }
}

class _InsightPanel extends StatelessWidget {
  const _InsightPanel({required this.insight});
  final Insight insight;

  int? _breakAt(List<double?> v) {
    int? at;
    var worst = 0.0;
    for (var i = 1; i < v.length; i++) {
      final a = v[i - 1], b = v[i];
      if (a == null || b == null) continue;
      if (a - b > worst) {
        worst = a - b;
        at = i - 1;
      }
    }
    return at;
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final i = insight;
    Widget step(int n, String label, Widget body) => Padding(
          padding: const EdgeInsets.only(bottom: 14),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Container(
              width: 22,
              height: 22,
              alignment: Alignment.center,
              decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: p.lineStrong)),
              child: Text('$n', style: textStyle(p, size: 11.5, weight: FontWeight.w600, color: p.textSecondary, height: 1)),
            ),
            const SizedBox(width: 10),
            Expanded(child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [Text(label, style: textStyle(p, size: 12.5, weight: FontWeight.w600, color: p.textTertiary, height: 1.4)), const SizedBox(height: 3), body])),
          ]),
        );
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 4),
      decoration: BoxDecoration(color: p.surfaceRaised, borderRadius: BorderRadius.circular(Radii.surface), border: Border.all(color: p.line)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Text(i.question, style: textStyle(p, size: 18, weight: FontWeight.w600, height: 1.4)),
        const SizedBox(height: 14),
        step(1, '关键变化', Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          Text(i.change, style: textStyle(p, size: TypeScale.dense, weight: FontWeight.w500)),
          const SizedBox(height: 8),
          Sparkline(i.series, height: 72, highlightFrom: _breakAt(i.series)),
          const SizedBox(height: 4),
          metaText(context, i.seriesLabel),
        ])),
        step(2, '原因', Column(crossAxisAlignment: CrossAxisAlignment.start, children: [for (final r in i.reasons) Padding(padding: const EdgeInsets.only(bottom: 4), child: Text('· $r', style: textStyle(p, size: TypeScale.dense)))])),
        step(3, '证据', Wrap(spacing: 6, runSpacing: 6, children: [for (final e in i.evidence) Tag(e, icon: OIcon.link)])),
        step(4, '行动', Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          for (final a in i.actions)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Padding(padding: const EdgeInsets.only(top: 3), child: Container(width: 12, height: 12, decoration: BoxDecoration(borderRadius: BorderRadius.circular(3), border: Border.all(color: p.lineStrong, width: 1.4)))),
                const SizedBox(width: 8),
                Expanded(child: Text(a, style: textStyle(p, size: TypeScale.dense))),
              ]),
            ),
        ])),
      ]),
    );
  }
}
