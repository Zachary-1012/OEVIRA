import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';

/// Icon-only action. Always carries a name for tooltips and screen readers and
/// keeps the 40×40 click area independent of the 20-unit glyph.
class IconAction extends StatelessWidget {
  const IconAction(this.icon, {super.key, required this.label, this.onPressed, this.selected = false, this.size = Targets.click, this.glyph = IconSizes.regular, this.color});

  final OIcon icon;
  final String label;
  final VoidCallback? onPressed;
  final bool selected;
  final double size;
  final double glyph;
  final Color? color;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Tooltip(
      message: label,
      child: Semantics(
        button: true,
        selected: selected,
        label: label,
        child: _Pressable(
          onPressed: onPressed,
          radius: Radii.control,
          fill: selected ? p.accentSoft : null,
          child: SizedBox.square(
            dimension: size,
            child: Center(child: LineIcon(icon, size: glyph, color: color ?? (selected ? p.accentText : p.textSecondary))),
          ),
        ),
      ),
    );
  }
}

enum ButtonTone { primary, standard, quiet, danger }

class OButton extends StatelessWidget {
  const OButton(this.label, {super.key, this.icon, this.onPressed, this.tone = ButtonTone.standard, this.dense = false, this.trailing});

  final String label;
  final OIcon? icon;
  final VoidCallback? onPressed;
  final ButtonTone tone;
  final bool dense;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final (bg, fg, border) = switch (tone) {
      ButtonTone.primary => (p.accent, p.onAccent, Colors.transparent),
      ButtonTone.standard => (p.surface, p.text, p.line),
      ButtonTone.quiet => (Colors.transparent, p.textSecondary, Colors.transparent),
      ButtonTone.danger => (p.dangerSoft, p.danger, Colors.transparent),
    };
    final disabled = onPressed == null;
    return Semantics(
      button: true,
      enabled: !disabled,
      child: _Pressable(
        onPressed: onPressed,
        radius: Radii.control,
        fill: bg,
        border: border,
        child: ConstrainedBox(
          constraints: BoxConstraints(minHeight: dense ? 32 : Targets.click),
          child: Padding(
            padding: EdgeInsets.symmetric(horizontal: dense ? 10 : 14),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (icon != null) ...[LineIcon(icon!, size: dense ? 16 : 18, color: disabled ? p.textTertiary : fg), const SizedBox(width: 6)],
                Text(label, style: textStyle(p, size: TypeScale.dense, weight: FontWeight.w500, color: disabled ? p.textTertiary : fg, height: 1.2)),
                if (trailing != null) ...[const SizedBox(width: 6), trailing!],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Hover, press and keyboard focus feedback shared by every custom control.
/// Feedback is immediate; no animation delays the first visible change.
class _Pressable extends StatefulWidget {
  const _Pressable({required this.child, required this.onPressed, required this.radius, this.fill, this.border});

  final Widget child;
  final VoidCallback? onPressed;
  final double radius;
  final Color? fill;
  final Color? border;

  @override
  State<_Pressable> createState() => _PressableState();
}

class _PressableState extends State<_Pressable> {
  bool _hover = false;
  bool _down = false;
  bool _focus = false;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final enabled = widget.onPressed != null;
    Color? overlay;
    if (enabled && _down) {
      overlay = p.text.withValues(alpha: 0.10);
    } else if (enabled && _hover) {
      overlay = p.text.withValues(alpha: 0.05);
    }
    return FocusableActionDetector(
      enabled: enabled,
      mouseCursor: enabled ? SystemMouseCursors.click : SystemMouseCursors.basic,
      onShowHoverHighlight: (v) => setState(() => _hover = v),
      onShowFocusHighlight: (v) => setState(() => _focus = v),
      actions: {ActivateIntent: CallbackAction<ActivateIntent>(onInvoke: (_) => widget.onPressed?.call())},
      child: GestureDetector(
        behavior: HitTestBehavior.opaque,
        onTapDown: enabled ? (_) => setState(() => _down = true) : null,
        onTapCancel: () => setState(() => _down = false),
        onTapUp: (_) => setState(() => _down = false),
        onTap: widget.onPressed,
        child: Container(
          decoration: BoxDecoration(
            color: widget.fill,
            borderRadius: BorderRadius.circular(widget.radius),
            border: Border.all(color: _focus ? p.focus : (widget.border ?? Colors.transparent), width: _focus ? 2 : 1),
          ),
          foregroundDecoration: overlay == null ? null : BoxDecoration(color: overlay, borderRadius: BorderRadius.circular(widget.radius)),
          child: widget.child,
        ),
      ),
    );
  }
}

/// A selectable row used in lists, indexes and navigation.
class RowAction extends StatelessWidget {
  const RowAction({super.key, required this.child, this.onPressed, this.selected = false, this.padding = const EdgeInsets.symmetric(horizontal: 10, vertical: 8)});

  final Widget child;
  final VoidCallback? onPressed;
  final bool selected;
  final EdgeInsets padding;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Semantics(
      button: onPressed != null,
      selected: selected,
      child: _Pressable(
        onPressed: onPressed,
        radius: Radii.control,
        fill: selected ? p.accentSoft : null,
        child: ConstrainedBox(constraints: const BoxConstraints(minHeight: Targets.click), child: Padding(padding: padding, child: child)),
      ),
    );
  }
}

class Tag extends StatelessWidget {
  const Tag(this.text, {super.key, this.tone, this.icon});
  final String text;
  final Color? tone;
  final OIcon? icon;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    final c = tone ?? p.textSecondary;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: tone == null ? p.surfaceSunken : c.withValues(alpha: p.isNight ? 0.18 : 0.10),
        borderRadius: BorderRadius.circular(Radii.pill),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        if (icon != null) ...[LineIcon(icon!, size: 12, color: c), const SizedBox(width: 4)],
        Text(text, style: textStyle(p, size: 12, weight: FontWeight.w500, color: c, height: 1.35)),
      ]),
    );
  }
}

/// Quiet marker that the visible content is illustrative.
class SampleMark extends StatelessWidget {
  const SampleMark({super.key, this.text = '示例'});
  final String text;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Semantics(
      label: '示例内容，不是真实数据',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
        decoration: BoxDecoration(border: Border.all(color: p.lineStrong.withValues(alpha: 0.6)), borderRadius: BorderRadius.circular(4)),
        child: Text(text, style: textStyle(p, size: 11, color: p.textTertiary, height: 1.4)),
      ),
    );
  }
}

class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key, this.trailing});
  final String text;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Semantics(
      header: true,
      child: Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Row(children: [
          Expanded(child: Text(text, style: textStyle(p, size: TypeScale.section, weight: FontWeight.w600, color: p.textSecondary, height: 1.4))),
          ?trailing,
        ]),
      ),
    );
  }
}

/// Solid content surface. Body text always sits on one of these.
class Pane extends StatelessWidget {
  const Pane({super.key, required this.child, this.padding = const EdgeInsets.all(Space.lg), this.color, this.radius = Radii.pane});
  final Widget child;
  final EdgeInsets padding;
  final Color? color;
  final double radius;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Container(
      decoration: BoxDecoration(
        color: color ?? p.surface,
        borderRadius: BorderRadius.circular(radius),
        border: Border.all(color: p.line),
      ),
      padding: padding,
      child: child,
    );
  }
}

class StatusDot extends StatelessWidget {
  const StatusDot(this.color, {super.key, this.size = 8});
  final Color color;
  final double size;

  @override
  Widget build(BuildContext context) => Container(width: size, height: size, decoration: BoxDecoration(color: color, shape: BoxShape.circle));
}

class KeyValue extends StatelessWidget {
  const KeyValue(this.label, this.value, {super.key, this.pending = false, this.labelWidth = 76});
  final String label;
  final String value;
  final bool pending;
  final double labelWidth;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 3),
      child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
        SizedBox(width: labelWidth, child: Text(label, style: textStyle(p, size: 13, color: p.textTertiary, height: 1.45))),
        Expanded(child: Text(value, style: textStyle(p, size: 13, color: pending ? p.warning : p.text, height: 1.45))),
      ]),
    );
  }
}

Text titleText(BuildContext context, String text, {double size = TypeScale.title, int? maxLines}) =>
    Text(text, maxLines: maxLines, overflow: maxLines == null ? null : TextOverflow.ellipsis, style: textStyle(context.palette, size: size, weight: FontWeight.w600, height: 1.35));

Text bodyText(BuildContext context, String text, {bool dense = false, Color? color, int? maxLines}) => Text(
      text,
      maxLines: maxLines,
      overflow: maxLines == null ? null : TextOverflow.ellipsis,
      style: textStyle(context.palette, size: dense ? TypeScale.dense : TypeScale.body, color: color),
    );

Text metaText(BuildContext context, String text, {int? maxLines}) => Text(
      text,
      maxLines: maxLines,
      overflow: maxLines == null ? null : TextOverflow.ellipsis,
      style: textStyle(context.palette, size: 12.5, color: context.palette.textTertiary, height: 1.4),
    );
