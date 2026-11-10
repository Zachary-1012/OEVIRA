import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../widgets/controls.dart';
import 'common.dart';

class _Row {
  const _Row(this.ref, this.title, this.type, this.owner, this.status, this.updated, this.icon);
  final ObjectRef ref;
  final String title;
  final String type;
  final String owner;
  final String status;
  final String updated;
  final OIcon icon;
}

enum _Filter { all, material, artifact, work, subject, monitor }

extension on _Filter {
  String get label => switch (this) {
        _Filter.all => '全部',
        _Filter.material => '资料',
        _Filter.artifact => '成果',
        _Filter.work => '工作',
        _Filter.subject => '主体',
        _Filter.monitor => '监测',
      };
  ObjectKind? get kind => switch (this) {
        _Filter.all => null,
        _Filter.material => ObjectKind.material,
        _Filter.artifact => ObjectKind.artifact,
        _Filter.work => ObjectKind.work,
        _Filter.subject => ObjectKind.subject,
        _Filter.monitor => ObjectKind.monitor,
      };
}

/// 资料与成果: every object in one sortable table, independent of where it sits
/// on any desk, so nothing depends on spatial memory.
class LibrarySurface extends StatefulWidget {
  const LibrarySurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;
  @override
  State<LibrarySurface> createState() => _LibrarySurfaceState();
}

class _LibrarySurfaceState extends State<LibrarySurface> {
  _Filter _filter = _Filter.all;
  String _query = '';

  List<_Row> _rows() {
    final c = widget.state.content;
    String owner(String? workId) => workId == null ? '—' : (c.work(workId)?.title ?? '—');
    return [
      for (final m in c.materials) _Row(ObjectRef(ObjectKind.material, m.id), m.title, m.kind.label, owner(m.workId ?? c.works.where((w) => w.materialIds.contains(m.id)).firstOrNull?.id), m.provenance.version, m.provenance.capturedAt, OIcon.research),
      for (final a in c.artifacts) _Row(ObjectRef(ObjectKind.artifact, a.id), a.title, a.kind.label, owner(a.workId), '第 ${a.version} 版 · ${a.status}', a.updated, iconForArtifact(a.kind)),
      for (final w in c.works) _Row(ObjectRef(ObjectKind.work, w.id), w.title, '工作', w.clientId == null ? '独立' : (c.client(w.clientId!)?.name ?? '—'), w.state.label, w.updated, OIcon.clients),
      for (final s in c.subjects) _Row(ObjectRef(ObjectKind.subject, s.id), s.name, s.kind.label, s.city, s.facts.any((f) => f.pending) ? '有待核事实' : '已核', '—', OIcon.subjects),
      for (final m in c.monitors) _Row(ObjectRef(ObjectKind.monitor, m.id), m.title, '监测', owner(m.workId), m.state.label, m.checks.isEmpty ? '—' : m.checks.first.lastChecked, OIcon.monitor),
    ];
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final all = _rows();
    final rows = all.where((r) => (_filter.kind == null || r.ref.kind == _filter.kind) && (_query.isEmpty || r.title.contains(_query) || r.owner.contains(_query))).toList();
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SurfaceHeader(state: widget.state, title: '资料与成果', context: '${all.length} 个对象 · 原件与成果分开保存，删除前会先进入回收区'),
      Row(children: [
        Expanded(
          child: SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(children: [
              for (final f in _Filter.values)
                Padding(padding: const EdgeInsets.only(right: 6), child: OButton('${f.label} ${f.kind == null ? all.length : all.where((r) => r.ref.kind == f.kind).length}', dense: true, tone: f == _filter ? ButtonTone.primary : ButtonTone.quiet, onPressed: () => setState(() => _filter = f))),
            ]),
          ),
        ),
        if (!widget.narrow)
          SizedBox(
            width: 240,
            child: TextField(
              onChanged: (v) => setState(() => _query = v.trim()),
              style: textStyle(p, size: 14, height: 1.3),
              decoration: const InputDecoration(hintText: '按名称或所属工作筛选', isDense: true),
            ),
          ),
      ]),
      const SizedBox(height: 10),
      Expanded(
        child: Pane(
          padding: EdgeInsets.zero,
          child: LayoutBuilder(builder: (context, box) {
            final wide = box.maxWidth >= 860;
            Widget head(String t, int flex) => Expanded(flex: flex, child: Text(t, style: textStyle(p, size: 12.5, weight: FontWeight.w600, color: p.textTertiary, height: 1.3)));
            return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              Container(
                padding: const EdgeInsets.fromLTRB(16, 10, 16, 10),
                decoration: BoxDecoration(border: Border(bottom: BorderSide(color: p.line))),
                child: Row(children: [head('名称', 5), head('类型', 2), if (wide) head('所属', 4), head('状态', 3), if (wide) head('更新', 2)]),
              ),
              Expanded(
                child: rows.isEmpty
                    ? Center(child: metaText(context, '没有符合条件的对象'))
                    : ListView.builder(
                        itemCount: rows.length,
                        itemBuilder: (context, n) {
                          final r = rows[n];
                          return RowAction(
                            selected: widget.state.selection == r.ref,
                            padding: const EdgeInsets.fromLTRB(16, 9, 16, 9),
                            onPressed: () => widget.state.open(r.ref),
                            child: Row(children: [
                              Expanded(
                                flex: 5,
                                child: Row(children: [
                                  LineIcon(r.icon, size: 16, color: p.textSecondary),
                                  const SizedBox(width: 8),
                                  Expanded(child: Text(r.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, weight: FontWeight.w500, height: 1.35))),
                                ]),
                              ),
                              Expanded(flex: 2, child: Text(r.type, maxLines: 1, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.35))),
                              if (wide) Expanded(flex: 4, child: Text(r.owner, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.35))),
                              Expanded(flex: 3, child: Text(r.status, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.35))),
                              if (wide) Expanded(flex: 2, child: Text(r.updated, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12.5, color: p.textTertiary, height: 1.35))),
                            ]),
                          );
                        },
                      ),
              ),
            ]);
          }),
        ),
      ),
    ]);
  }
}
