import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'common.dart';

/// 浏览器: tabs, address and a reader. Saving a page turns it into a material
/// with source and capture time on its back face.
class BrowserSurface extends StatefulWidget {
  const BrowserSurface({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;
  @override
  State<BrowserSurface> createState() => _BrowserSurfaceState();
}

class _BrowserSurfaceState extends State<BrowserSurface> {
  int _tab = 0;
  bool _reader = true;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final pages = widget.state.content.browserPages;
    final page = pages[_tab];
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      SurfaceHeader(state: widget.state, title: '浏览器', context: '页面在寰意内打开，收进资料时保留地址与取得时间'),
      Expanded(
        child: Pane(
          padding: EdgeInsets.zero,
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            Container(
              height: 44,
              padding: const EdgeInsets.symmetric(horizontal: 8),
              decoration: BoxDecoration(color: p.surfaceSunken, border: Border(bottom: BorderSide(color: p.line)), borderRadius: const BorderRadius.vertical(top: Radius.circular(Radii.pane))),
              child: Row(children: [
                for (var n = 0; n < pages.length; n++)
                  Flexible(
                    child: Semantics(
                      selected: n == _tab,
                      button: true,
                      child: GestureDetector(
                        onTap: () => setState(() => _tab = n),
                        child: MouseRegion(
                          cursor: SystemMouseCursors.click,
                          child: Container(
                            constraints: const BoxConstraints(maxWidth: 260),
                            margin: const EdgeInsets.only(top: 6, right: 4),
                            padding: const EdgeInsets.symmetric(horizontal: 12),
                            decoration: BoxDecoration(color: n == _tab ? p.surface : Colors.transparent, borderRadius: const BorderRadius.vertical(top: Radius.circular(8)), border: n == _tab ? Border(top: BorderSide(color: p.line), left: BorderSide(color: p.line), right: BorderSide(color: p.line)) : null),
                            child: Row(children: [
                              LineIcon(OIcon.browser, size: 14, color: p.textTertiary),
                              const SizedBox(width: 6),
                              Flexible(child: Text(pages[n].title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 12.5, weight: n == _tab ? FontWeight.w600 : FontWeight.w400, height: 1.2))),
                            ]),
                          ),
                        ),
                      ),
                    ),
                  ),
                IconAction(OIcon.plus, label: '新标签页', size: 32, glyph: 16, onPressed: () => widget.state.setOverlay(ShellLayer.search)),
              ]),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 8, 8, 8),
              child: Row(children: [
                IconAction(OIcon.back, label: '后退', onPressed: _tab > 0 ? () => setState(() => _tab--) : null),
                IconAction(OIcon.refresh, label: '重新载入', onPressed: () => showNotice(context, notConnected)),
                const SizedBox(width: 6),
                Expanded(
                  child: Container(
                    height: 36,
                    padding: const EdgeInsets.symmetric(horizontal: 12),
                    decoration: BoxDecoration(color: p.surfaceSunken, borderRadius: BorderRadius.circular(Radii.pill), border: Border.all(color: p.line)),
                    child: Row(children: [
                      LineIcon(OIcon.link, size: 14, color: p.textTertiary),
                      const SizedBox(width: 8),
                      Expanded(child: Text(page.address, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13.5, color: p.textSecondary, height: 1.2))),
                      const Tag('示例页面'),
                    ]),
                  ),
                ),
                const SizedBox(width: 6),
                IconAction(OIcon.reader, label: _reader ? '退出阅读模式' : '阅读模式', selected: _reader, onPressed: () => setState(() => _reader = !_reader)),
                if (!widget.narrow) OButton('收进资料', icon: OIcon.stow, dense: true, onPressed: () => showNotice(context, notConnected)),
              ]),
            ),
            Divider(height: 1, color: p.line),
            Expanded(
              child: Container(
                color: _reader ? p.paper : p.surface,
                child: ListView(padding: EdgeInsets.symmetric(horizontal: widget.narrow ? 18 : 48, vertical: 28), children: [
                  Center(
                    child: ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 680),
                      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                        Text(page.title, style: textStyle(p, size: 24, weight: FontWeight.w600, height: 1.4)),
                        const SizedBox(height: 6),
                        metaText(context, '${page.address} · 示例页面，不是真实网站'),
                        const SizedBox(height: 20),
                        for (final s in page.sections) ...[
                          Text(s.label, style: textStyle(p, size: 13, weight: FontWeight.w600, color: p.textTertiary, height: 1.4)),
                          const SizedBox(height: 4),
                          SelectableText(s.text, style: textStyle(p, size: s.heading ? 18 : TypeScale.body, weight: s.heading ? FontWeight.w600 : FontWeight.w400, height: 1.65)),
                          const SizedBox(height: 18),
                        ],
                      ]),
                    ),
                  ),
                ]),
              ),
            ),
          ]),
        ),
      ),
    ]);
  }
}
