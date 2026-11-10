import 'dart:math' as math;

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
import 'object_faces.dart';

enum _ResearchView { list, desk }

/// Research keeps original materials, the argument draft and evidence on one
/// desk. Opening a material brings the same instance square-on for reading.
class ResearchSurface extends StatefulWidget {
  const ResearchSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  State<ResearchSurface> createState() => _ResearchSurfaceState();
}

class _ResearchSurfaceState extends State<ResearchSurface> {
  String? _reading;
  // Narrow windows open on the readable index; the desk is one tap away.
  late _ResearchView _view = widget.narrow ? _ResearchView.list : _ResearchView.desk;
  final Map<String, double> _readerOffsets = {};

  AppState get s => widget.state;

  static const _workId = 'w-research';

  SceneController get _scene => s.scene('research', () {
        final work = s.content.work(_workId)!;
        final items = <SpatialInstance>[];
        // Two piles that fit the narrowest docked desk (~560): materials are
        // fanned so every grip strip and title stays visible, and the draft
        // lies beside them. Nothing starts outside the visible desk.
        for (var n = 0; n < work.materialIds.length; n++) {
          items.add(SpatialInstance(
            id: 'desk-${work.materialIds[n]}',
            ref: ObjectRef(ObjectKind.material, work.materialIds[n]),
            pose: Pose(Offset(16 + n * 6.0, 16 + n * 46.0), turn: const [-0.02, 0.015, -0.01, 0.02, -0.015, 0.01][n % 6]),
            size: const Size(252, 292),
            stowed: n >= 5,
          ));
        }
        items.add(const SpatialInstance(id: 'desk-a-article', ref: ObjectRef(ObjectKind.artifact, 'a-article'), pose: Pose(Offset(290, 16)), size: Size(256, 430)));
        return items;
      });

  void _open(SpatialInstance i) {
    if (i.ref.kind != ObjectKind.material) {
      s.select(i.ref);
      return;
    }
    setState(() => _reading = i.ref.id);
    s.select(i.ref);
  }

  @override
  Widget build(BuildContext context) {
    final work = s.content.work(_workId)!;
    final materials = work.materialIds.map(s.content.material).whereType<SourceMaterial>().toList();
    final reading = _reading == null ? null : s.content.material(_reading!);
    return LayoutBuilder(builder: (context, constraints) {
      final roomy = constraints.maxWidth >= 860;
      final showIndex = !widget.narrow && constraints.maxWidth >= 700;
      return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        SurfaceHeader(
          state: s,
          title: work.title,
          context: '研究 · 独立研究，不属于客户 · ${materials.length} 份资料 · 下一步：${work.nextStep}',
          actions: [
            if (widget.narrow)
              Segmented<_ResearchView>(values: _ResearchView.values, selected: _view, label: (v) => v == _ResearchView.list ? '索引' : '桌面', onChanged: (v) => setState(() => _view = v))
            else ...[
              OButton('添加资料', icon: OIcon.plus, dense: true, onPressed: () => s.setOverlay(ShellLayer.create)),
              OButton('专注', icon: OIcon.focus, dense: true, tone: ButtonTone.quiet, onPressed: s.toggleFocus),
            ],
          ],
        ),
        Expanded(
          child: Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            if (showIndex) ...[SizedBox(width: 248, child: _MaterialIndex(state: s, materials: materials, scene: _scene, reading: _reading, onRead: (id) => setState(() => _reading = id))), const SizedBox(width: Space.dockGap)],
            Expanded(
              child: reading != null
                  ? Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                      Expanded(
                        child: _Reader(
                          key: ValueKey(reading.id),
                          state: s,
                          material: reading,
                          initialOffset: _readerOffsets[reading.id] ?? 0,
                          onOffset: (o) => _readerOffsets[reading.id] = o,
                          onClose: () => setState(() => _reading = null),
                        ),
                      ),
                      if (roomy) ...[const SizedBox(width: Space.dockGap), SizedBox(width: math.min(340, constraints.maxWidth * 0.34), child: _DraftPane(state: s))],
                    ])
                  : (widget.narrow && _view == _ResearchView.list)
                      ? _MaterialIndex(state: s, materials: materials, scene: _scene, reading: _reading, onRead: (id) => setState(() => _reading = id))
                      : Desk(
                          controller: _scene,
                          faces: researchFaces(s),
                          info: (ref) => shellInfo(s, ref),
                          onOpen: _open,
                          onSelect: (i) => s.select(i?.ref),
                        ),
            ),
          ]),
        ),
      ]);
    });
  }
}

/// Keyboard- and screen-reader reachable index of every desk object, including
/// stowed ones. Finding something never depends on remembering its position.
class _MaterialIndex extends StatelessWidget {
  const _MaterialIndex({required this.state, required this.materials, required this.scene, required this.reading, required this.onRead});
  final AppState state;
  final List<SourceMaterial> materials;
  final SceneController scene;
  final String? reading;
  final void Function(String id) onRead;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Pane(
      padding: const EdgeInsets.fromLTRB(8, 12, 8, 8),
      child: ValueListenableBuilder<SceneState>(
        valueListenable: scene,
        builder: (context, sceneState, _) => ListView(children: [
          Padding(padding: const EdgeInsets.symmetric(horizontal: 6), child: SectionLabel('资料索引 · ${materials.length}')),
          for (final m in materials)
            Builder(builder: (context) {
              final inst = sceneState.instances['desk-${m.id}'];
              final selected = state.selection == ObjectRef(ObjectKind.material, m.id);
              return Padding(
                padding: const EdgeInsets.only(bottom: 2),
                child: RowAction(
                  selected: selected || reading == m.id,
                  onPressed: () => onRead(m.id),
                  child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Padding(padding: const EdgeInsets.only(top: 2), child: LineIcon(OIcon.research, size: 16, color: p.textSecondary)),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(m.title, maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4)),
                        const SizedBox(height: 2),
                        Text('${m.kind.label} · ${m.provenance.version}${inst?.stowed == true ? ' · 已收纳' : ''}', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3)),
                      ]),
                    ),
                    if (inst != null)
                      IconAction(
                        inst.stowed ? OIcon.open : OIcon.front,
                        label: inst.stowed ? '取出到桌面' : '带到最前',
                        size: 32,
                        glyph: 16,
                        onPressed: () => scene.dispatch(inst.stowed ? Recall(inst.id, const Size(900, 640)) : BringToFront(inst.id)),
                      ),
                  ]),
                ),
              );
            }),
          const SizedBox(height: 12),
          const Padding(padding: EdgeInsets.symmetric(horizontal: 6), child: SectionLabel('作品')),
          RowAction(
            onPressed: () => scene.dispatch(const BringToFront('desk-a-article')),
            child: Row(children: [
              LineIcon(OIcon.text, size: 16, color: p.textSecondary),
              const SizedBox(width: 8),
              Expanded(child: Text(state.content.artifact('a-article')!.title, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.4))),
            ]),
          ),
          const SizedBox(height: 12),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 6),
            child: metaText(context, '拿起：按住物件顶部条拖动，或聚焦后用方向键。F 翻面，S 收纳，P 固定，[ ] 转向。'),
          ),
        ]),
      ),
    );
  }
}

class _Reader extends StatefulWidget {
  const _Reader({super.key, required this.state, required this.material, required this.onClose, required this.initialOffset, required this.onOffset});
  final AppState state;
  final SourceMaterial material;
  final VoidCallback onClose;
  final double initialOffset;
  final void Function(double) onOffset;

  @override
  State<_Reader> createState() => _ReaderState();
}

class _ReaderState extends State<_Reader> {
  late final ScrollController _scroll = ScrollController(initialScrollOffset: widget.initialOffset);
  bool _back = false;

  @override
  void dispose() {
    widget.onOffset(_scroll.hasClients ? _scroll.offset : widget.initialOffset);
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final m = widget.material;
    final s = widget.state;
    return Container(
      decoration: BoxDecoration(
        color: p.paper,
        borderRadius: BorderRadius.circular(Radii.pane),
        border: Border.all(color: p.objectEdge),
        boxShadow: [BoxShadow(color: p.contactShadow, blurRadius: 10, offset: const Offset(0, 4))],
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Container(
          padding: const EdgeInsets.fromLTRB(8, 6, 8, 6),
          decoration: BoxDecoration(border: Border(bottom: BorderSide(color: p.line))),
          child: Row(children: [
            IconAction(OIcon.back, label: '放回桌面', onPressed: widget.onClose),
            const SizedBox(width: 4),
            Expanded(child: Text(m.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 15, weight: FontWeight.w600, height: 1.3))),
            Tag(m.provenance.version),
            const SizedBox(width: 6),
            OButton(_back ? '正面 · 原文' : '背面 · 来源', icon: OIcon.flip, dense: true, onPressed: () => setState(() => _back = !_back)),
          ]),
        ),
        Expanded(
          child: _back
              ? ListView(padding: const EdgeInsets.all(20), children: [provenanceFace(context, m, expanded: true)])
              : Scrollbar(
                  controller: _scroll,
                  child: ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.fromLTRB(28, 22, 28, 28),
                    itemCount: m.pages.length + 1,
                    itemBuilder: (context, n) {
                      if (n == 0) {
                        return Padding(padding: const EdgeInsets.only(bottom: 16), child: Text(m.summary, style: textStyle(p, size: TypeScale.dense, color: p.textSecondary)));
                      }
                      final seg = m.pages[n - 1];
                      final cited = s.researchCitations.contains(seg.text);
                      return Padding(
                        padding: const EdgeInsets.only(bottom: 18),
                        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                          SizedBox(width: 132, child: Padding(padding: const EdgeInsets.only(top: 3), child: Text(seg.label, style: textStyle(p, size: 12.5, weight: FontWeight.w600, color: p.textTertiary, height: 1.4)))),
                          Expanded(
                            child: SelectableText(seg.text, style: textStyle(p, size: TypeScale.body, weight: seg.heading ? FontWeight.w500 : FontWeight.w400)),
                          ),
                          const SizedBox(width: 8),
                          IconAction(cited ? OIcon.check : OIcon.link, label: cited ? '已引用到论证稿，点按移除' : '引用此段到论证稿', selected: cited, size: 36, glyph: 16, onPressed: () => cited ? s.removeCitation(seg.text) : s.addCitation(seg.text)),
                        ]),
                      );
                    },
                  ),
                ),
        ),
      ]),
    );
  }
}

class _DraftPane extends StatelessWidget {
  const _DraftPane({required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    final a = state.content.artifact('a-article')!;
    return Pane(padding: EdgeInsets.zero, child: articleFace(context, state, a, dense: false));
  }
}
