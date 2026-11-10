import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../brand/brand_art.dart';
import '../brand/line_icons.dart';
import '../content/models.dart';
import '../navigation/destinations.dart';
import '../state/app_state.dart';
import '../surfaces/surfaces.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';
import 'command_layers.dart';
import 'tool_panel.dart';

enum ShellWidth { wide, medium, narrow }

ShellWidth shellWidthFor(double width) => width >= Breakpoints.wide
    ? ShellWidth.wide
    : width >= Breakpoints.medium
        ? ShellWidth.medium
        : ShellWidth.narrow;

class WorkspaceShell extends StatelessWidget {
  const WorkspaceShell({super.key, required this.state});

  final AppState state;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Shortcuts(
      shortcuts: const {
        SingleActivator(LogicalKeyboardKey.keyK, control: true): _SearchIntent(),
        SingleActivator(LogicalKeyboardKey.keyK, meta: true): _SearchIntent(),
        SingleActivator(LogicalKeyboardKey.keyN, control: true, shift: true): _CreateIntent(),
        SingleActivator(LogicalKeyboardKey.backslash, control: true): _NavIntent(),
        SingleActivator(LogicalKeyboardKey.period, control: true): _ToolIntent(),
      },
      child: Actions(
        actions: {
          _SearchIntent: CallbackAction<_SearchIntent>(onInvoke: (_) => state.setOverlay(ShellLayer.search)),
          _CreateIntent: CallbackAction<_CreateIntent>(onInvoke: (_) => state.setOverlay(ShellLayer.create)),
          _NavIntent: CallbackAction<_NavIntent>(onInvoke: (_) => state.toggleNav()),
          _ToolIntent: CallbackAction<_ToolIntent>(onInvoke: (_) => state.toggleTool()),
        },
        child: Focus(
          autofocus: true,
          child: DecoratedBox(
            // The content layer's floor: stable ambient light plus one key light
            // from the upper left, so the glass layers above have something to
            // pass through (HIG Materials).
            decoration: BoxDecoration(
              gradient: LinearGradient(begin: Alignment.topLeft, end: Alignment.bottomRight, colors: [p.floorTop, p.space, p.floorBottom], stops: const [0, 0.45, 1]),
            ),
            child: LayoutBuilder(builder: (context, constraints) {
              final width = shellWidthFor(constraints.maxWidth);
              return Stack(children: [
                const Positioned.fill(child: IgnorePointer(child: _KeyLight())),
                Positioned.fill(child: _Frame(state: state, width: width, size: constraints.biggest)),
                if (state.overlay != ShellLayer.none) Positioned.fill(child: CommandLayer(state: state)),
              ]);
            }),
          ),
        ),
      ),
    );
  }
}

/// Soft key light pooled at the upper left of the floor. It is what makes the
/// translucent navigation and tool layers read as material rather than paint.
class _KeyLight extends StatelessWidget {
  const _KeyLight();

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return DecoratedBox(
      decoration: BoxDecoration(
        gradient: RadialGradient(
          center: const Alignment(-0.85, -1.1),
          radius: 0.9,
          colors: [p.isNight ? const Color(0x1F8DB5FF) : const Color(0xB3FFFFFF), const Color(0x00FFFFFF)],
        ),
      ),
    );
  }
}

class _SearchIntent extends Intent {
  const _SearchIntent();
}

class _CreateIntent extends Intent {
  const _CreateIntent();
}

class _NavIntent extends Intent {
  const _NavIntent();
}

class _ToolIntent extends Intent {
  const _ToolIntent();
}

class _Frame extends StatelessWidget {
  const _Frame({required this.state, required this.width, required this.size});

  final AppState state;
  final ShellWidth width;
  final Size size;

  @override
  Widget build(BuildContext context) {
    final focus = state.focusMode;
    final wide = width == ShellWidth.wide;
    final narrow = width == ShellWidth.narrow;
    final margin = narrow ? 8.0 : Space.outerMargin;
    final gap = narrow ? 8.0 : Space.dockGap;

    // Wide docks navigation and tools beside the content. Medium keeps
    // navigation docked and lifts tools into a short layer. Narrow keeps the
    // content and recalls both as layers over it.
    state.navDockable = !narrow;
    state.toolDockable = wide && _toolFits(size.width);
    final dockNav = !focus && state.navDockable && state.navOpen;
    final dockTool = !focus && state.toolDockable && state.toolOpen;
    final floatTool = !focus && !state.toolDockable && state.toolDrawer;
    final floatNav = !focus && !state.navDockable && state.navDrawer;

    final content = Surfaces(state: state, narrow: narrow);

    return Column(children: [
      TitleBar(state: state, width: width),
      Expanded(
        child: Padding(
          padding: EdgeInsets.fromLTRB(margin, 0, margin, margin),
          child: Stack(children: [
            Row(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
              if (dockNav) ...[
                SizedBox(width: width == ShellWidth.medium ? Dock.navMin : state.navWidth, child: NavPanel(state: state)),
                _Divider(onDrag: (d) => state.setNavWidth(state.navWidth + d), onEnd: state.commitDock, label: '调整导航宽度', onStep: (d) {
                  state.setNavWidth(state.navWidth + d);
                  state.commitDock();
                }, gap: gap),
              ],
              Expanded(child: content),
              if (dockTool) ...[
                _Divider(onDrag: (d) => state.setToolWidth(state.toolWidth - d), onEnd: state.commitDock, label: '调整工具栏宽度', onStep: (d) {
                  state.setToolWidth(state.toolWidth - d);
                  state.commitDock();
                }, gap: gap),
                SizedBox(width: state.toolWidth, child: ToolPanel(state: state)),
              ],
            ]),
            if (floatTool)
              Positioned(
                top: 0,
                bottom: 0,
                right: 0,
                width: narrow ? size.width - margin * 2 : Dock.toolDefault,
                child: _Layer(child: ToolPanel(state: state, floating: true)),
              ),
            if (floatNav)
              Positioned(top: 0, bottom: 0, left: 0, width: 280, child: _Layer(child: NavPanel(state: state, floating: true))),
          ]),
        ),
      ),
    ]);
  }

  bool _toolFits(double total) {
    final nav = state.navOpen ? state.navWidth + Space.dockGap : 0;
    return total - Space.outerMargin * 2 - nav - state.toolWidth - Space.dockGap >= Dock.contentMin;
  }
}

class _Layer extends StatelessWidget {
  const _Layer({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(Radii.pane),
        boxShadow: [BoxShadow(color: p.liftShadow, blurRadius: 40, offset: const Offset(0, 18))],
      ),
      child: child,
    );
  }
}

class _Divider extends StatelessWidget {
  const _Divider({required this.onDrag, required this.onEnd, required this.onStep, required this.label, required this.gap});

  final void Function(double dx) onDrag;
  final VoidCallback onEnd;
  final void Function(double dx) onStep;
  final String label;
  final double gap;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Semantics(
      label: label,
      hint: '左右方向键调整',
      slider: true,
      child: FocusableActionDetector(
        mouseCursor: SystemMouseCursors.resizeColumn,
        shortcuts: const {
          SingleActivator(LogicalKeyboardKey.arrowLeft): _StepIntent(-16),
          SingleActivator(LogicalKeyboardKey.arrowRight): _StepIntent(16),
        },
        actions: {_StepIntent: CallbackAction<_StepIntent>(onInvoke: (i) => onStep(i.dx))},
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          onHorizontalDragUpdate: (d) => onDrag(d.delta.dx),
          onHorizontalDragEnd: (_) => onEnd(),
          child: SizedBox(
            width: gap,
            child: Center(child: Container(width: 2, height: 36, decoration: BoxDecoration(color: p.line, borderRadius: BorderRadius.circular(1)))),
          ),
        ),
      ),
    );
  }
}

class _StepIntent extends Intent {
  const _StepIntent(this.dx);
  final double dx;
}

/// Translucent chrome material, used only where no body text sits beneath it.
class ChromeMaterial extends StatelessWidget {
  const ChromeMaterial({super.key, required this.child, this.radius = Radii.pane, this.border = true});
  final Widget child;
  final double radius;
  final bool border;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final r = BorderRadius.circular(radius);
    // Liquid-Glass-like functional layer: blur of what lies beneath, a light
    // tint that is brighter at the lit edge, a 1px inner highlight and an
    // outer hairline. Body text never sits on it without its own solid base.
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: r,
        boxShadow: [BoxShadow(color: p.contactShadow.withValues(alpha: p.isNight ? 0.35 : 0.07), blurRadius: 18, offset: const Offset(0, 6))],
      ),
      child: ClipRRect(
        borderRadius: r,
        child: BackdropFilter(
          filter: ui.ImageFilter.blur(sigmaX: 30, sigmaY: 30),
          child: Container(
            decoration: BoxDecoration(
              borderRadius: r,
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [Color.alphaBlend(p.glassEdge.withValues(alpha: p.isNight ? 0.06 : 0.35), p.chrome), p.chrome],
              ),
              border: border ? Border.all(color: p.chromeLine) : null,
            ),
            foregroundDecoration: border ? BoxDecoration(borderRadius: r, border: Border(top: BorderSide(color: p.glassEdge, width: 1))) : null,
            child: child,
          ),
        ),
      ),
    );
  }
}

class TitleBar extends StatelessWidget {
  const TitleBar({super.key, required this.state, required this.width});
  final AppState state;
  final ShellWidth width;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final narrow = width == ShellWidth.narrow;
    final night = p.isNight;
    return SizedBox(
      height: Dock.titleBar,
      child: Padding(
        padding: EdgeInsets.symmetric(horizontal: narrow ? 8 : Space.outerMargin - 4),
        child: Row(children: [
          IconAction(OIcon.navPanel, label: state.navShown ? '收起导航' : '展开导航', selected: state.navShown, onPressed: state.toggleNav),
          const SizedBox(width: 6),
          OeviraLogo(size: 24, color: p.text, accent: p.accent),
          const SizedBox(width: 8),
          if (!narrow) Text('寰意', style: textStyle(p, size: 16, weight: FontWeight.w600, height: 1.2)),
          if (!narrow) ...[
            const SizedBox(width: 12),
            _WorkspaceChip(name: state.content.workspaceName, sample: state.content.isSample),
          ],
          const SizedBox(width: 12),
          Expanded(
            child: Align(
              alignment: narrow ? Alignment.centerLeft : Alignment.center,
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520),
                child: _SearchField(onTap: () => state.setOverlay(ShellLayer.search), compact: narrow),
              ),
            ),
          ),
          const SizedBox(width: 8),
          if (!narrow) OButton('新建/打开', icon: OIcon.plus, dense: true, onPressed: () => state.setOverlay(ShellLayer.create)) else IconAction(OIcon.plus, label: '新建或打开', onPressed: () => state.setOverlay(ShellLayer.create)),
          const SizedBox(width: 4),
          IconAction(OIcon.browser, label: '浏览器', selected: state.destination == Destination.browser, onPressed: () => state.go(Destination.browser)),
          IconAction(OIcon.ai, label: 'AI 对话', selected: state.toolShown && state.toolTab == ToolTab.ai, onPressed: () {
            if (state.toolShown && state.toolTab == ToolTab.ai) {
              state.toggleTool();
            } else {
              state.showTool(ToolTab.ai);
            }
          }),
          if (!narrow) IconAction(OIcon.toolPanel, label: state.toolShown ? '收起工具栏' : '展开工具栏', selected: state.toolShown, onPressed: state.toggleTool),
          IconAction(night ? OIcon.sun : OIcon.moon, label: night ? '切换到白天外观' : '切换到夜间外观', onPressed: () => state.setAppearance(night ? Appearance.day : Appearance.night)),
        ]),
      ),
    );
  }
}

class _WorkspaceChip extends StatelessWidget {
  const _WorkspaceChip({required this.name, required this.sample});
  final String name;
  final bool sample;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Row(mainAxisSize: MainAxisSize.min, children: [
      Container(width: 1, height: 18, color: p.line),
      const SizedBox(width: 12),
      Text(name, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.2)),
      if (sample) ...[const SizedBox(width: 6), const SampleMark(text: '示例内容')],
    ]);
  }
}

class _SearchField extends StatelessWidget {
  const _SearchField({required this.onTap, required this.compact});
  final VoidCallback onTap;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return RowAction(
      onPressed: onTap,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 0),
      child: Container(
        height: 36,
        decoration: BoxDecoration(
          color: p.surface.withValues(alpha: p.isNight ? 0.55 : 0.72),
          borderRadius: BorderRadius.circular(Radii.pill),
          border: Border.all(color: p.chromeLine),
        ),
        padding: const EdgeInsets.symmetric(horizontal: 14),
        child: Row(children: [
          LineIcon(OIcon.search, size: 16, color: p.textTertiary),
          const SizedBox(width: 8),
          Expanded(child: Text(compact ? '搜索' : '搜索工作、资料、作品与主体', maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textTertiary, height: 1.2))),
          if (!compact)
            Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
              decoration: BoxDecoration(color: p.selection, borderRadius: BorderRadius.circular(5)),
              child: Text('Ctrl K', style: textStyle(p, size: 11.5, color: p.textTertiary, height: 1.3)),
            ),
        ]),
      ),
    );
  }
}

class NavPanel extends StatelessWidget {
  const NavPanel({super.key, required this.state, this.floating = false});
  final AppState state;
  final bool floating;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final c = state.content;
    String? badge(Destination d) => switch (d) {
          Destination.clients => '${c.clients.length}',
          Destination.monitoring => '${c.monitors.expand((m) => m.events).where((e) => !e.handled).length}',
          _ => null,
        };
    return ChromeMaterial(
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        if (floating)
          Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 4, 0),
            child: Row(children: [
              Expanded(child: Text('导航', style: textStyle(p, size: 15, weight: FontWeight.w600))),
              IconAction(OIcon.close, label: '关闭导航', onPressed: state.toggleNav),
            ]),
          ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(8, 10, 8, 8),
            children: [
              for (final group in NavGroup.values) ...[
                Padding(
                  padding: const EdgeInsets.fromLTRB(10, 12, 10, 4),
                  child: Semantics(header: true, child: Text(group.label, style: textStyle(p, size: 11.5, weight: FontWeight.w600, color: p.textTertiary, height: 1.3))),
                ),
                for (final d in Destination.inGroup(group))
                  _NavItem(
                    destination: d,
                    selected: state.destination == d,
                    badge: badge(d),
                    onTap: () {
                      state.go(d);
                      if (floating) state.toggleNav();
                    },
                  ),
              ],
              const SizedBox(height: 10),
              Padding(padding: const EdgeInsets.symmetric(horizontal: 10), child: Divider(height: 1, color: p.chromeLine)),
              const SizedBox(height: 8),
              Padding(
                padding: const EdgeInsets.fromLTRB(10, 0, 10, 4),
                child: Text('最近', style: textStyle(p, size: 12, weight: FontWeight.w600, color: p.textTertiary, height: 1.3)),
              ),
              for (final w in c.works.take(3))
                RowAction(
                  onPressed: () {
                    state.go(Destination.clients);
                    state.select(ObjectRef(ObjectKind.work, w.id));
                  },
                  child: Text(w.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.3)),
                ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
          child: Column(children: [
            _NavFooter(icon: OIcon.apps, label: '全部功能', onTap: () => state.setOverlay(ShellLayer.search)),
            _NavFooter(icon: OIcon.link, label: '技能与连接', onTap: () => state.setOverlay(ShellLayer.search)),
            _NavFooter(icon: OIcon.settings, label: '设置与帮助', onTap: () => state.setOverlay(ShellLayer.search)),
          ]),
        ),
      ]),
    );
  }
}

class _NavItem extends StatelessWidget {
  const _NavItem({required this.destination, required this.selected, required this.onTap, this.badge});
  final Destination destination;
  final bool selected;
  final VoidCallback onTap;
  final String? badge;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 1),
      child: RowAction(
        selected: selected,
        onPressed: onTap,
        child: Row(children: [
          LineIcon(destination.icon, size: 20, color: selected ? p.accentText : p.textSecondary, accent: selected ? p.accent.withValues(alpha: 0.22) : null),
          const SizedBox(width: 10),
          Expanded(
            child: Text(destination.label, style: textStyle(p, size: TypeScale.dense, weight: selected ? FontWeight.w600 : FontWeight.w400, color: p.text, height: 1.3)),
          ),
          if (badge != null)
            Container(
              constraints: const BoxConstraints(minWidth: 20),
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
              decoration: BoxDecoration(color: p.selection, borderRadius: BorderRadius.circular(Radii.pill)),
              child: Text(badge!, textAlign: TextAlign.center, style: textStyle(p, size: 11.5, weight: FontWeight.w500, color: p.textSecondary, height: 1.35)),
            ),
        ]),
      ),
    );
  }
}

class _NavFooter extends StatelessWidget {
  const _NavFooter({required this.icon, required this.label, required this.onTap});
  final OIcon icon;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return RowAction(
      onPressed: onTap,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      child: Row(children: [
        LineIcon(icon, size: 18, color: p.textSecondary),
        const SizedBox(width: 10),
        Text(label, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.3)),
      ]),
    );
  }
}
