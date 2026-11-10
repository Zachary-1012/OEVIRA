import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../brand/line_icons.dart';
import '../content/models.dart';
import '../navigation/destinations.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';

class _Hit {
  const _Hit(this.title, this.detail, this.icon, this.action, {this.group = '对象'});
  final String title;
  final String detail;
  final OIcon icon;
  final VoidCallback action;
  final String group;
}

/// Global search and new/open share one short-lived layer with a single focus
/// owner. Escape closes it and returns to the previous work.
class CommandLayer extends StatefulWidget {
  const CommandLayer({super.key, required this.state});
  final AppState state;

  @override
  State<CommandLayer> createState() => _CommandLayerState();
}

class _CommandLayerState extends State<CommandLayer> {
  final TextEditingController _query = TextEditingController();
  final FocusNode _field = FocusNode();
  int _active = 0;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _field.requestFocus());
  }

  @override
  void dispose() {
    _query.dispose();
    _field.dispose();
    super.dispose();
  }

  void _close() => widget.state.setOverlay(ShellLayer.none);

  List<_Hit> _hits() {
    final s = widget.state;
    final c = s.content;
    final q = _query.text.trim().toLowerCase();
    bool match(String text) => q.isEmpty || text.toLowerCase().contains(q);
    final hits = <_Hit>[];
    if (s.overlay == ShellLayer.create) {
      const types = <(String, String, OIcon, Destination)>[
        ('研究', '提出问题、打开资料并形成判断', OIcon.research, Destination.research),
        ('写作', '文章、命名、多语言文稿', OIcon.text, Destination.creation),
        ('Word / PPT / 工作簿', '按目标和意见生成与改版文件', OIcon.reader, Destination.creation),
        ('PDF / 动态 HTML', '可筛选的报告与看板', OIcon.reader, Destination.creation),
        ('视频工程', '多轨剪辑、字幕、色彩与混音', OIcon.create, Destination.creation),
        ('播客工程', '录音、take、词稿与章节', OIcon.create, Destination.creation),
        ('监测', '持续关注品牌、活动或话题', OIcon.monitor, Destination.monitoring),
        ('主体', '按名称或材料新增品牌、场所或门店', OIcon.subjects, Destination.subjects),
      ];
      for (final t in types) {
        if (match(t.$1)) hits.add(_Hit('新建${t.$1}', t.$2, t.$3, () => s.go(t.$4), group: '新建'));
      }
      for (final w in c.works) {
        if (match(w.title)) hits.add(_Hit(w.title, '打开工作 · ${w.state.label} · ${w.updated}', OIcon.open, () => s.open(ObjectRef(ObjectKind.work, w.id)), group: '打开'));
      }
      return hits;
    }
    for (final d in Destination.values) {
      if (match(d.label) || match(d.purpose)) hits.add(_Hit(d.label, d.purpose, d.icon, () => s.go(d), group: '工作面'));
    }
    for (final w in c.works) {
      if (match(w.title) || match(w.goal)) hits.add(_Hit(w.title, '工作 · ${w.state.label}', OIcon.clients, () => s.open(ObjectRef(ObjectKind.work, w.id))));
    }
    for (final m in c.materials) {
      final inText = q.isNotEmpty && m.pages.any((seg) => seg.text.toLowerCase().contains(q));
      if (match(m.title) || inText) hits.add(_Hit(m.title, inText ? '资料 · 正文命中' : '资料 · ${m.kind.label}', OIcon.research, () => s.open(ObjectRef(ObjectKind.material, m.id))));
    }
    for (final a in c.artifacts) {
      if (match(a.title)) hits.add(_Hit(a.title, '${a.kind.label} · 第 ${a.version} 版', OIcon.create, () => s.open(ObjectRef(ObjectKind.artifact, a.id))));
    }
    for (final sub in c.subjects) {
      if (match(sub.name)) hits.add(_Hit(sub.name, '主体 · ${sub.kind.label}', OIcon.subjects, () => s.open(ObjectRef(ObjectKind.subject, sub.id))));
    }
    for (final m in c.monitors) {
      if (match(m.title)) hits.add(_Hit(m.title, '监测 · ${m.state.label}', OIcon.monitor, () => s.open(ObjectRef(ObjectKind.monitor, m.id))));
    }
    for (final r in c.releases) {
      if (match(r.title)) hits.add(_Hit(r.title, '社区 · ${r.kind.label}', OIcon.community, () => s.open(ObjectRef(ObjectKind.release, r.id))));
    }
    return hits;
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final hits = _hits();
    final creating = widget.state.overlay == ShellLayer.create;
    if (_active >= hits.length) _active = hits.isEmpty ? 0 : hits.length - 1;
    String? lastGroup;
    return Stack(children: [
      Positioned.fill(child: GestureDetector(onTap: _close, child: ColoredBox(color: Colors.black.withValues(alpha: p.isNight ? 0.45 : 0.18)))),
      Align(
        alignment: const Alignment(0, -0.55),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 640, maxHeight: 520),
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Material(
              color: p.surfaceRaised,
              elevation: 0,
              shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Radii.pane), side: BorderSide(color: p.line)),
              clipBehavior: Clip.antiAlias,
              child: CallbackShortcuts(
                bindings: {
                  const SingleActivator(LogicalKeyboardKey.escape): _close,
                  const SingleActivator(LogicalKeyboardKey.arrowDown): () => setState(() => _active = (_active + 1).clamp(0, hits.isEmpty ? 0 : hits.length - 1)),
                  const SingleActivator(LogicalKeyboardKey.arrowUp): () => setState(() => _active = (_active - 1).clamp(0, hits.isEmpty ? 0 : hits.length - 1)),
                },
                child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
                    child: Row(children: [
                      LineIcon(creating ? OIcon.plus : OIcon.search, color: p.textSecondary),
                      const SizedBox(width: 10),
                      Expanded(
                        child: TextField(
                          controller: _query,
                          focusNode: _field,
                          style: textStyle(p, size: TypeScale.body),
                          decoration: InputDecoration(
                            hintText: creating ? '新建什么，或打开哪项工作' : '搜索工作、资料正文、作品、主体与功能',
                            filled: false,
                            border: InputBorder.none,
                            enabledBorder: InputBorder.none,
                            focusedBorder: InputBorder.none,
                          ),
                          onChanged: (_) => setState(() => _active = 0),
                          onSubmitted: (_) {
                            if (hits.isEmpty) return;
                            _close();
                            hits[_active].action();
                          },
                        ),
                      ),
                      IconAction(OIcon.close, label: '关闭', onPressed: _close),
                    ]),
                  ),
                  Divider(height: 1, color: p.line),
                  Flexible(
                    child: hits.isEmpty
                        ? Padding(padding: const EdgeInsets.all(24), child: Text('没有找到“${_query.text}”。可以换个词，或在研究中打开资料后再搜。', style: textStyle(p, size: TypeScale.dense, color: p.textSecondary)))
                        : ListView.builder(
                            shrinkWrap: true,
                            padding: const EdgeInsets.all(8),
                            itemCount: hits.length,
                            itemBuilder: (context, n) {
                              final h = hits[n];
                              final header = h.group != lastGroup;
                              lastGroup = h.group;
                              return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                                if (header) Padding(padding: const EdgeInsets.fromLTRB(10, 8, 10, 4), child: Text(h.group, style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.textTertiary, height: 1.3))),
                                RowAction(
                                  selected: n == _active,
                                  onPressed: () {
                                    _close();
                                    h.action();
                                  },
                                  child: Row(children: [
                                    LineIcon(h.icon, size: 18, color: p.textSecondary),
                                    const SizedBox(width: 10),
                                    Expanded(child: Text(h.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: TypeScale.dense, weight: FontWeight.w500, height: 1.3))),
                                    const SizedBox(width: 8),
                                    Flexible(child: Text(h.detail, maxLines: 1, overflow: TextOverflow.ellipsis, textAlign: TextAlign.end, style: textStyle(p, size: 12.5, color: p.textTertiary, height: 1.3))),
                                  ]),
                                ),
                              ]);
                            },
                          ),
                  ),
                  Container(
                    padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
                    color: p.surfaceSunken,
                    child: metaText(context, '↑↓ 选择 · Enter 打开 · Esc 关闭${widget.state.content.isSample ? ' · 当前为示例内容' : ''}'),
                  ),
                ]),
              ),
            ),
          ),
        ),
      ),
    ]);
  }
}
