import 'package:flutter/material.dart';

import '../brand/brand_art.dart';
import '../brand/line_icons.dart';
import '../content/content_source.dart';
import '../content/models.dart';
import '../navigation/destinations.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'workspace_shell.dart';

/// Related tools for the current object: the AI conversation and an inspector
/// of the selected object's version, source and state.
class ToolPanel extends StatelessWidget {
  const ToolPanel({super.key, required this.state, this.floating = false});
  final AppState state;
  final bool floating;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    // On 工作台 the conversation is the main surface; a second copy beside it
    // would only repeat it, so the tool layer shows the inspector there.
    final chatIsMain = state.destination == Destination.workbench;
    final tab = chatIsMain ? ToolTab.inspector : state.toolTab;
    return ChromeMaterial(
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 8, 4, 4),
          child: Row(children: [
            if (!chatIsMain) ...[
              _Tab(label: 'AI 对话', icon: OIcon.ai, selected: tab == ToolTab.ai, onTap: () => state.showTool(ToolTab.ai)),
              const SizedBox(width: 4),
            ],
            _Tab(label: '检查器', icon: OIcon.reader, selected: tab == ToolTab.inspector, onTap: () => state.showTool(ToolTab.inspector)),
            const Spacer(),
            IconAction(OIcon.close, label: '收起工具栏', onPressed: state.toggleTool, size: 36, glyph: 18),
          ]),
        ),
        Divider(height: 1, color: p.chromeLine),
        Expanded(child: tab == ToolTab.ai ? AiConversation(state: state, conversationKey: state.destination.name) : Inspector(state: state)),
      ]),
    );
  }
}

class _Tab extends StatelessWidget {
  const _Tab({required this.label, required this.icon, required this.selected, required this.onTap});
  final String label;
  final OIcon icon;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return RowAction(
      selected: selected,
      onPressed: onTap,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        LineIcon(icon, size: 16, color: selected ? p.accentText : p.textSecondary),
        const SizedBox(width: 6),
        Text(label, style: textStyle(p, size: 13, weight: selected ? FontWeight.w600 : FontWeight.w500, color: selected ? p.text : p.textSecondary, height: 1.2)),
      ]),
    );
  }
}

/// Chat is the main delegation entry. It always shows what the next message
/// will refer to, and the composer keeps unsent text across navigation.
class AiConversation extends StatefulWidget {
  const AiConversation({super.key, required this.state, required this.conversationKey, this.large = false});
  final AppState state;
  final String conversationKey;
  final bool large;

  @override
  State<AiConversation> createState() => _AiConversationState();
}

class _AiConversationState extends State<AiConversation> {
  late final TextEditingController _input = TextEditingController(text: widget.state.drafts[widget.conversationKey] ?? '');
  final ScrollController _scroll = ScrollController();

  @override
  void didUpdateWidget(AiConversation old) {
    super.didUpdateWidget(old);
    if (old.conversationKey != widget.conversationKey) {
      widget.state.drafts[old.conversationKey] = _input.text;
      _input.text = widget.state.drafts[widget.conversationKey] ?? '';
    }
  }

  @override
  void dispose() {
    widget.state.drafts[widget.conversationKey] = _input.text;
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _send() {
    final text = _input.text.trim();
    if (text.isEmpty) return;
    final ref = widget.state.selection;
    widget.state.sendMessage(widget.conversationKey, text, refs: [?ref]);
    _input.clear();
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (_scroll.hasClients) _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final state = widget.state;
    final messages = state.conversation(widget.conversationKey);
    final ref = state.selection;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Expanded(
        child: messages.isEmpty
            ? _Empty(destination: state.destination.label)
            : ListView.separated(
                controller: _scroll,
                padding: EdgeInsets.symmetric(horizontal: widget.large ? 28 : 14, vertical: widget.large ? 24 : 14),
                itemCount: messages.length,
                separatorBuilder: (_, _) => SizedBox(height: widget.large ? 22 : 16),
                itemBuilder: (context, n) => Center(
                  child: ConstrainedBox(constraints: const BoxConstraints(maxWidth: 720), child: _Message(message: messages[n], state: state)),
                ),
              ),
      ),
      Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 760),
          child: Padding(
        padding: EdgeInsets.fromLTRB(widget.large ? 20 : 10, 0, widget.large ? 20 : 10, widget.large ? 18 : 10),
        // ChatGPT composer: the largest control on the surface, a faint
        // outline and a soft drop, the send button a solid circle.
        child: Container(
          decoration: BoxDecoration(
            color: p.surfaceRaised,
            borderRadius: BorderRadius.circular(widget.large ? 26 : 20),
            border: Border.all(color: p.isNight ? p.line : p.line),
            boxShadow: [BoxShadow(color: p.contactShadow.withValues(alpha: p.isNight ? 0.5 : 0.08), blurRadius: 16, offset: const Offset(0, 4))],
          ),
          padding: EdgeInsets.fromLTRB(widget.large ? 18 : 14, 10, 8, 8),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Flexible(
                child: Container(
                  padding: const EdgeInsets.fromLTRB(8, 3, 8, 3),
                  decoration: BoxDecoration(color: ref == null ? p.selection : p.accentSoft, borderRadius: BorderRadius.circular(Radii.pill)),
                  child: Row(mainAxisSize: MainAxisSize.min, children: [
                    LineIcon(OIcon.link, size: 13, color: ref == null ? p.textTertiary : p.accentText),
                    const SizedBox(width: 5),
                    Flexible(
                      child: Text(
                        ref == null ? '范围：当前工作面，未选对象' : '引用：${state.content.titleOf(ref)}',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: textStyle(p, size: 12, color: ref == null ? p.textSecondary : p.accentText, height: 1.3),
                      ),
                    ),
                  ]),
                ),
              ),
              if (ref != null) IconAction(OIcon.close, label: '移除引用', size: 28, glyph: 14, onPressed: () => state.select(null)),
            ]),
            TextField(
              controller: _input,
              minLines: widget.large ? 2 : 1,
              maxLines: 6,
              style: textStyle(p, size: TypeScale.dense),
              decoration: const InputDecoration(hintText: '描述目标，或对选中的对象提出修改', filled: false, border: InputBorder.none, enabledBorder: InputBorder.none, focusedBorder: InputBorder.none, contentPadding: EdgeInsets.symmetric(vertical: 8)),
              onSubmitted: (_) => _send(),
            ),
            Row(children: [
              metaText(context, 'Enter 发送'),
              const Spacer(),
              Tooltip(
                message: '发送',
                child: Semantics(
                  button: true,
                  label: '发送',
                  child: GestureDetector(
                    onTap: _send,
                    child: MouseRegion(
                      cursor: SystemMouseCursors.click,
                      child: Container(
                        width: 36,
                        height: 36,
                        decoration: BoxDecoration(color: p.text, shape: BoxShape.circle),
                        child: Center(child: LineIcon(OIcon.send, size: 18, color: p.surface)),
                      ),
                    ),
                  ),
                ),
              ),
            ]),
          ]),
        ),
      ),
        ),
      ),
    ]);
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.destination});
  final String destination;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(mainAxisSize: MainAxisSize.min, children: [
          CompanionMark(Companion.xun, size: 32, color: p.accentText),
          const SizedBox(height: 12),
          Text('在$destination里说出你要完成的事', textAlign: TextAlign.center, style: textStyle(p, size: 17, weight: FontWeight.w600, height: 1.35)),
          const SizedBox(height: 4),
          Text('选中的对象会作为引用随消息发出', textAlign: TextAlign.center, style: textStyle(p, size: 13, color: p.textTertiary)),
        ]),
      ),
    );
  }
}

class _Message extends StatelessWidget {
  const _Message({required this.message, required this.state});
  final ChatMessage message;
  final AppState state;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final user = message.role == ChatRole.user;
    final system = message.role == ChatRole.system;
    // ChatGPT hierarchy: my words in a soft neutral bubble on the right; the
    // assistant answers as plain text on the page, no box around it.
    final bubble = Container(
      padding: user || system ? const EdgeInsets.symmetric(horizontal: 14, vertical: 10) : const EdgeInsets.only(left: 2, right: 2),
      decoration: user || system
          ? BoxDecoration(
              color: user ? (p.isNight ? p.surfaceRaised : const Color(0xFFF0F1F3)) : p.warningSoft,
              borderRadius: BorderRadius.circular(20),
            )
          : null,
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (system) Padding(padding: const EdgeInsets.only(bottom: 4), child: Row(children: [LineIcon(OIcon.alert, size: 14, color: p.warning), const SizedBox(width: 6), Text('未连接服务', style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.warning, height: 1.3))])),
        Text(message.text, style: textStyle(p, size: user || system ? TypeScale.dense : 15, height: user || system ? null : 1.6)),
        for (final ref in message.refs)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Semantics(
              button: true,
              child: GestureDetector(
                onTap: () => state.open(ref),
                child: MouseRegion(
                  cursor: SystemMouseCursors.click,
                  child: Container(
                    constraints: const BoxConstraints(minHeight: 30),
                    padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                    decoration: BoxDecoration(color: p.surface, borderRadius: BorderRadius.circular(Radii.pill), border: Border.all(color: p.line)),
                    child: Row(mainAxisSize: MainAxisSize.min, children: [
                      LineIcon(OIcon.link, size: 13, color: p.accentText),
                      const SizedBox(width: 5),
                      Flexible(child: Text(state.content.titleOf(ref), maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12.5, color: p.text, height: 1.3))),
                    ]),
                  ),
                ),
              ),
            ),
          ),
      ]),
    );
    return Align(
      alignment: user ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: user ? 520 : 720),
        child: Column(crossAxisAlignment: user ? CrossAxisAlignment.end : CrossAxisAlignment.start, children: [
          if (!user && !system) Padding(padding: const EdgeInsets.only(bottom: 6, left: 2), child: Row(mainAxisSize: MainAxisSize.min, children: [OeviraLogo(size: 18, color: p.text, accent: p.accent), const SizedBox(width: 8), Text('寰意 AI', style: textStyle(p, size: 13, weight: FontWeight.w600, height: 1.3)), const SizedBox(width: 6), const SampleMark()])),
          bubble,
        ]),
      ),
    );
  }
}

class Inspector extends StatelessWidget {
  const Inspector({super.key, required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    final ref = state.selection;
    final c = state.content;
    if (ref == null) return _InspectorOverview(state: state);
    final children = <Widget>[];
    void section(String title, List<Widget> rows) {
      children
        ..add(Padding(padding: const EdgeInsets.only(top: 14), child: SectionLabel(title)))
        ..addAll(rows);
    }

    children.add(titleText(context, c.titleOf(ref), size: 17));
    switch (ref.kind) {
      case ObjectKind.material:
        final m = c.material(ref.id)!;
        children.add(Padding(padding: const EdgeInsets.only(top: 6), child: Wrap(spacing: 6, runSpacing: 6, children: [Tag(m.kind.label), for (final t in m.tags) Tag(t)])));
        section('版本与来源', [
          KeyValue('版本', m.provenance.version),
          KeyValue('来源', m.provenance.source),
          KeyValue('取得时间', m.provenance.capturedAt),
          KeyValue('方法', m.provenance.method),
          KeyValue('许可', m.provenance.license),
          KeyValue('指纹', m.provenance.fingerprint),
          if (m.provenance.limitations != null) KeyValue('限制', m.provenance.limitations!, pending: true),
        ]);
      case ObjectKind.work:
        final w = c.work(ref.id)!;
        section('当前状态', [KeyValue('状态', w.state.label), KeyValue('更新', w.updated), KeyValue('下一步', w.nextStep)]);
        section('已确认条件', [for (final k in w.conditions) KeyValue(k.label, k.confirmed ? k.value : '${k.value} · 待确认', pending: !k.confirmed)]);
      case ObjectKind.artifact:
        final a = c.artifact(ref.id)!;
        section('版本', [KeyValue('类型', a.kind.label), KeyValue('当前', '第 ${a.version} 版'), KeyValue('状态', a.status), KeyValue('更新', a.updated)]);
        if (a.comments.isNotEmpty) section('意见', [for (final r in a.comments) KeyValue(r.author, '${r.anchor}：${r.text}${r.resolved ? '（已处理）' : ''}')]);
      case ObjectKind.subject:
        final s = c.subject(ref.id)!;
        section('身份', [KeyValue('类型', s.kind.label), KeyValue('城市', s.city)]);
        section('有据事实', [for (final f in s.facts) KeyValue(f.label, '${f.value} · ${f.source}', pending: f.pending)]);
      case ObjectKind.monitor:
        final m = c.monitor(ref.id)!;
        section('检查状态', [for (final k in m.checks) KeyValue(k.source, '${k.lastChecked} · ${k.result}', pending: k.result.contains('过期') || k.result.contains('暂停'))]);
      case ObjectKind.release:
        final r = c.release(ref.id)!;
        section('发布版', [KeyValue('作者', r.author), KeyValue('类型', r.kind.label), KeyValue('发布', r.published), KeyValue('许可', r.license)]);
      case ObjectKind.client:
        final cl = c.client(ref.id)!;
        section('客户边界', [KeyValue('联系人', cl.contact), KeyValue('可见范围', cl.boundary)]);
      case ObjectKind.note:
        break;
    }
    return ListView(padding: const EdgeInsets.all(14), children: children);
  }
}


/// Inspector with nothing selected: what this surface is for, what it holds
/// and how to reach things without the pointer. One UI grouped lists on a
/// solid base, so the text never sits directly on glass.
class _InspectorOverview extends StatelessWidget {
  const _InspectorOverview({required this.state});
  final AppState state;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final c = state.content;
    final d = state.destination;
    final counts = <(String, String)>[
      ('工作', '${c.works.length}'),
      ('原始资料', '${c.materials.length}'),
      ('作品与文件', '${c.artifacts.length}'),
      ('主体', '${c.subjects.length}'),
      ('监测', '${c.monitors.length}'),
    ];
    const keys = <(String, String)>[
      ('搜索', 'Ctrl K'),
      ('新建或打开', 'Ctrl Shift N'),
      ('导航', 'Ctrl \\'),
      ('工具栏', 'Ctrl .'),
    ];
    Widget group(List<Widget> rows) => Container(
          decoration: BoxDecoration(color: p.surface.withValues(alpha: p.isNight ? 0.7 : 0.82), borderRadius: BorderRadius.circular(Radii.surface)),
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Column(children: [
            for (var n = 0; n < rows.length; n++) ...[
              if (n > 0) Padding(padding: const EdgeInsets.only(left: 14), child: Divider(height: 1, color: p.line)),
              rows[n],
            ],
          ]),
        );
    Widget row(String a, String b, {bool key = false}) => Padding(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          child: Row(children: [
            Expanded(child: Text(a, style: textStyle(p, size: 13.5, height: 1.3))),
            key
                ? Container(
                    padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                    decoration: BoxDecoration(color: p.selection, borderRadius: BorderRadius.circular(6)),
                    child: Text(b, style: textStyle(p, size: 12, weight: FontWeight.w500, color: p.textSecondary, height: 1.3)),
                  )
                : Text(b, style: textStyle(p, size: 13.5, color: p.textSecondary, height: 1.3)),
          ]),
        );
    return ListView(padding: const EdgeInsets.fromLTRB(14, 16, 14, 16), children: [
      Row(children: [
        Container(
          width: 36,
          height: 36,
          decoration: BoxDecoration(color: p.accentSoft, borderRadius: BorderRadius.circular(10)),
          child: Center(child: LineIcon(d.icon, size: 20, color: p.accentText)),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(d.label, style: textStyle(p, size: 16, weight: FontWeight.w600, height: 1.3)),
            Text(d.purpose, style: textStyle(p, size: 12.5, color: p.textSecondary, height: 1.4)),
          ]),
        ),
      ]),
      const SizedBox(height: 14),
      Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(color: p.surface.withValues(alpha: p.isNight ? 0.7 : 0.82), borderRadius: BorderRadius.circular(Radii.surface)),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          LineIcon(OIcon.reader, size: 18, color: p.textSecondary),
          const SizedBox(width: 10),
          Expanded(child: Text('未选对象。选中一份资料、作品或工作，这里显示它的版本、来源、条件与意见。', style: textStyle(p, size: 13, color: p.textSecondary, height: 1.5))),
        ]),
      ),
      const SizedBox(height: 18),
      Padding(padding: const EdgeInsets.only(left: 4, bottom: 6), child: Row(children: [Text('本工作区', style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.textTertiary, height: 1.3)), const SizedBox(width: 6), if (c.isSample) const SampleMark()])),
      group([for (final (a, b) in counts) row(a, b)]),
      const SizedBox(height: 18),
      Padding(padding: const EdgeInsets.only(left: 4, bottom: 6), child: Text('键盘', style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.textTertiary, height: 1.3))),
      group([for (final (a, b) in keys) row(a, b, key: true)]),
    ]);
  }
}
