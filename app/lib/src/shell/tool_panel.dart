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
        Text(label, style: textStyle(p, size: 13, weight: FontWeight.w600, color: selected ? p.accentText : p.textSecondary, height: 1.2)),
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
                padding: const EdgeInsets.all(12),
                itemCount: messages.length,
                separatorBuilder: (_, _) => const SizedBox(height: 10),
                itemBuilder: (context, n) => _Message(message: messages[n], state: state),
              ),
      ),
      Padding(
        padding: const EdgeInsets.fromLTRB(10, 0, 10, 10),
        child: Container(
          decoration: BoxDecoration(color: p.surface, borderRadius: BorderRadius.circular(Radii.surface), border: Border.all(color: p.line)),
          padding: const EdgeInsets.fromLTRB(10, 8, 6, 6),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              LineIcon(OIcon.link, size: 14, color: p.textTertiary),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  ref == null ? '范围：当前工作面，未选对象' : '引用：${state.content.titleOf(ref)}',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: textStyle(p, size: 12, color: p.textTertiary, height: 1.3),
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
              IconAction(OIcon.send, label: '发送', selected: true, size: 36, glyph: 18, onPressed: _send),
            ]),
          ]),
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
          const SizedBox(height: 10),
          Text('在$destination里说出你要完成的事', textAlign: TextAlign.center, style: textStyle(p, size: TypeScale.dense, weight: FontWeight.w600)),
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
    final bubble = Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
      decoration: BoxDecoration(
        color: user ? p.accentSoft : (system ? p.warningSoft : p.surface),
        borderRadius: BorderRadius.circular(Radii.surface),
        border: user ? null : Border.all(color: system ? Colors.transparent : p.line),
      ),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (system) Padding(padding: const EdgeInsets.only(bottom: 4), child: Row(children: [LineIcon(OIcon.alert, size: 14, color: p.warning), const SizedBox(width: 6), Text('未连接服务', style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.warning, height: 1.3))])),
        Text(message.text, style: textStyle(p, size: TypeScale.dense, color: system ? p.text : p.text)),
        for (final ref in message.refs)
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: RowAction(
              onPressed: () => state.open(ref),
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
              child: Row(mainAxisSize: MainAxisSize.min, children: [
                LineIcon(OIcon.link, size: 13, color: p.accentText),
                const SizedBox(width: 4),
                Flexible(child: Text(state.content.titleOf(ref), maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12, color: p.accentText, height: 1.3))),
              ]),
            ),
          ),
      ]),
    );
    return Align(
      alignment: user ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 560),
        child: Column(crossAxisAlignment: user ? CrossAxisAlignment.end : CrossAxisAlignment.start, children: [
          if (!user && !system) Padding(padding: const EdgeInsets.only(bottom: 4, left: 2), child: Row(mainAxisSize: MainAxisSize.min, children: [OeviraLogo(size: 14, color: p.textSecondary, accent: p.accent), const SizedBox(width: 6), metaText(context, '寰意 AI'), const SizedBox(width: 6), const SampleMark()])),
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
    if (ref == null) {
      return Center(child: Padding(padding: const EdgeInsets.all(24), child: metaText(context, '选中一个物件，这里会显示它的版本、来源与当前状态。')));
    }
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

