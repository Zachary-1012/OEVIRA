import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../scene/desk.dart';
import '../scene/scene_model.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

ObjectShellInfo shellInfo(AppState s, ObjectRef ref) {
  final c = s.content;
  final label = switch (ref.kind) {
    ObjectKind.material => c.material(ref.id)?.kind.label ?? '资料',
    ObjectKind.artifact => c.artifact(ref.id)?.kind.label ?? '作品',
    ObjectKind.work => '工作',
    ObjectKind.monitor => '监测',
    ObjectKind.subject => '主体',
    ObjectKind.client => '客户',
    ObjectKind.release => '作品',
    ObjectKind.note => '便笺',
  };
  final icon = ref.kind == ObjectKind.artifact ? iconForArtifact(c.artifact(ref.id)?.kind ?? ArtifactKind.article) : iconForKind(ref.kind);
  return ObjectShellInfo(c.titleOf(ref), label, icon: icon);
}

/// Face registry for every object kind that can sit on a desk.
Map<ObjectKind, FaceBuilder> researchFaces(AppState s) => {
      ObjectKind.material: (context, i, face) {
        final m = s.content.material(i.ref.id)!;
        return face == Face.front ? _materialFront(context, m) : _scrollFace(provenanceFace(context, m));
      },
      ObjectKind.artifact: (context, i, face) {
        final a = s.content.artifact(i.ref.id)!;
        return face == Face.front ? (a.kind == ArtifactKind.article ? articleFace(context, s, a) : artifactFront(context, a)) : _scrollFace(artifactBack(context, a));
      },
      ObjectKind.work: (context, i, face) {
        final w = s.content.work(i.ref.id)!;
        return face == Face.front ? _workFront(context, s, w) : _scrollFace(_workBack(context, w));
      },
      ObjectKind.monitor: (context, i, face) {
        final m = s.content.monitor(i.ref.id)!;
        return face == Face.front ? _monitorFront(context, m) : _scrollFace(_monitorBack(context, m));
      },
    };

Widget _scrollFace(Widget child) => SingleChildScrollView(padding: const EdgeInsets.all(12), child: child);

Widget _materialFront(BuildContext context, SourceMaterial m) {
  final p = context.palette;
  return Padding(
    padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Wrap(spacing: 6, runSpacing: 4, children: [Tag(m.kind.label, tone: p.accentText), for (final t in m.tags.take(2)) Tag(t)]),
      const SizedBox(height: 8),
      Text(m.summary, maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.45)),
      const SizedBox(height: 8),
      Expanded(
        child: ClipRect(
          child: SingleChildScrollView(
            physics: const NeverScrollableScrollPhysics(),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (final seg in m.pages.skip(1).take(3))
              Padding(
                padding: const EdgeInsets.only(bottom: 6),
                child: Text.rich(
                  TextSpan(children: [
                    TextSpan(text: '${seg.label}  ', style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.textTertiary)),
                    TextSpan(text: seg.text, style: textStyle(p, size: 13, height: 1.45)),
                  ]),
                  maxLines: 3,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
          ]),
          ),
        ),
      ),
      Row(children: [
        metaText(context, '${m.pages.length} 段 · ${m.provenance.version.split(' ').first}'),
        const Spacer(),
        metaText(context, 'Enter 打开阅读'),
      ]),
    ]),
  );
}

/// Back face content: real provenance fields, never a mirrored front.
Widget provenanceFace(BuildContext context, SourceMaterial m, {bool expanded = false}) {
  final p = context.palette;
  return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
    Row(children: [LineIcon(OIcon.history, size: 16, color: p.accentText), const SizedBox(width: 6), Expanded(child: Text('版本 · 来源 · 方法 · 许可', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, weight: FontWeight.w600, height: 1.3)))]),
    const SizedBox(height: 8),
    KeyValue('版本', m.provenance.version, labelWidth: expanded ? 96 : 52),
    KeyValue('来源', m.provenance.source, labelWidth: expanded ? 96 : 52),
    KeyValue('取得', m.provenance.capturedAt, labelWidth: expanded ? 96 : 52),
    KeyValue('方法', m.provenance.method, labelWidth: expanded ? 96 : 52),
    KeyValue('许可', m.provenance.license, labelWidth: expanded ? 96 : 52),
    KeyValue('指纹', m.provenance.fingerprint, labelWidth: expanded ? 96 : 52),
    if (m.provenance.limitations != null) KeyValue('限制', m.provenance.limitations!, pending: true, labelWidth: expanded ? 96 : 52),
  ]);
}

Widget articleFace(BuildContext context, AppState s, Artifact a, {bool dense = true}) {
  final p = context.palette;
  return ListenableBuilder(
    listenable: s,
    builder: (context, _) => ListView(padding: const EdgeInsets.fromLTRB(14, 12, 14, 12), children: [
      Row(children: [Tag('第 ${a.version} 版'), const SizedBox(width: 6), Expanded(child: metaText(context, a.status, maxLines: 1))]),
      const SizedBox(height: 10),
      Text(a.title, style: textStyle(p, size: dense ? 16 : 18, weight: FontWeight.w600, height: 1.35)),
      const SizedBox(height: 8),
      for (final line in a.outline) Padding(padding: const EdgeInsets.only(bottom: 4), child: Text(line, style: textStyle(p, size: 13.5, color: p.textSecondary, height: 1.45))),
      const SizedBox(height: 10),
      SectionLabel('本次引用 · ${s.researchCitations.length}'),
      if (s.researchCitations.isEmpty)
        metaText(context, '在原文中点“引用此段”，原话会带着出处放到这里。')
      else
        for (final c in s.researchCitations)
          Container(
            margin: const EdgeInsets.only(bottom: 6),
            padding: const EdgeInsets.fromLTRB(10, 6, 4, 6),
            decoration: BoxDecoration(border: Border(left: BorderSide(color: p.accent, width: 2.5)), color: p.accentSoft.withValues(alpha: 0.5)),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Expanded(child: Text(c, style: textStyle(p, size: 13, height: 1.45))),
              IconAction(OIcon.close, label: '移除这条引用', size: 28, glyph: 14, onPressed: () => s.removeCitation(c)),
            ]),
          ),
    ]),
  );
}

Widget artifactFront(BuildContext context, Artifact a) {
  final p = context.palette;
  return Padding(
    padding: const EdgeInsets.all(12),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [Tag(a.kind.label, tone: p.accentText), const SizedBox(width: 6), Tag('第 ${a.version} 版')]),
      const SizedBox(height: 8),
      metaText(context, a.status),
      const SizedBox(height: 8),
      Expanded(
        child: ClipRect(
          child: SingleChildScrollView(
            physics: const NeverScrollableScrollPhysics(),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            for (var n = 0; n < a.outline.length; n++)
              Padding(padding: const EdgeInsets.only(bottom: 3), child: Text('${n + 1}  ${a.outline[n]}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, height: 1.45))),
          ]),
          ),
        ),
      ),
      if (a.comments.any((c) => !c.resolved)) Tag('${a.comments.where((c) => !c.resolved).length} 条待处理意见', tone: p.warning, icon: OIcon.alert),
    ]),
  );
}

Widget artifactBack(BuildContext context, Artifact a) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      KeyValue('类型', a.kind.label, labelWidth: 52),
      KeyValue('当前', '第 ${a.version} 版 · ${a.updated}', labelWidth: 52),
      KeyValue('状态', a.status, labelWidth: 52),
      for (final c in a.comments) KeyValue(c.author, '${c.anchor} · ${c.text}', labelWidth: 52, pending: !c.resolved),
    ]);

Widget _workFront(BuildContext context, AppState s, Work w) {
  final p = context.palette;
  return Padding(
    padding: const EdgeInsets.all(12),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [
        StatusDot(workStateColor(p, w.state)),
        const SizedBox(width: 6),
        Text(w.state.label, style: textStyle(p, size: 12.5, weight: FontWeight.w600, color: workStateColor(p, w.state), height: 1.3)),
        const Spacer(),
        metaText(context, w.updated),
      ]),
      const SizedBox(height: 8),
      Text(w.goal, maxLines: 3, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.45)),
      const Spacer(),
      Container(
        padding: const EdgeInsets.all(8),
        decoration: BoxDecoration(color: p.surfaceSunken, borderRadius: BorderRadius.circular(Radii.control)),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          LineIcon(OIcon.chevron, size: 14, color: p.accentText),
          const SizedBox(width: 4),
          Expanded(child: Text(w.nextStep, maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12.5, height: 1.4))),
        ]),
      ),
      const SizedBox(height: 6),
      metaText(context, '${w.materialIds.length} 份资料 · ${w.artifactIds.length} 件成果${w.clientId != null ? ' · ${s.content.client(w.clientId!)?.name}' : ' · 独立工作'}'),
    ]),
  );
}

Widget _workBack(BuildContext context, Work w) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      for (final k in w.conditions) KeyValue(k.label, k.value, pending: !k.confirmed, labelWidth: 64),
      if (w.conditions.isEmpty) metaText(context, '暂无已确认条件'),
      const SizedBox(height: 8),
      for (final t in w.timeline.take(3)) KeyValue(t.time, '${t.actor} · ${t.text}', labelWidth: 64),
    ]);

Widget _monitorFront(BuildContext context, Monitor m) {
  final p = context.palette;
  final open = m.events.where((e) => !e.handled).toList();
  return Padding(
    padding: const EdgeInsets.all(12),
    child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Row(children: [StatusDot(m.state == MonitorState.failed ? p.danger : (m.state == MonitorState.paused ? p.textTertiary : p.positive)), const SizedBox(width: 6), Text(m.state.label, style: textStyle(p, size: 12.5, weight: FontWeight.w600, height: 1.3))]),
      const SizedBox(height: 6),
      metaText(context, m.scope, maxLines: 1),
      const SizedBox(height: 8),
      for (final e in open.take(2))
        Padding(padding: const EdgeInsets.only(bottom: 6), child: Text('${e.time}  ${e.title}', maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, height: 1.45))),
      if (open.isEmpty) metaText(context, '没有新的待处理事件'),
    ]),
  );
}

Widget _monitorBack(BuildContext context, Monitor m) => Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      KeyValue('关键词', m.keywords.join('、'), labelWidth: 56),
      for (final c in m.checks) KeyValue(c.source, '${c.lastChecked} · ${c.result}', labelWidth: 56, pending: c.result.contains('过期')),
    ]);
