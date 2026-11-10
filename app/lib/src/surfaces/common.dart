import 'package:flutter/material.dart';

import '../brand/line_icons.dart';
import '../content/models.dart';
import '../state/app_state.dart';
import '../theme/theme.dart';
import '../theme/tokens.dart';
import '../widgets/controls.dart';

/// Content header: current work, its boundary, and the surface's own actions.
class SurfaceHeader extends StatelessWidget {
  const SurfaceHeader({super.key, required this.state, required this.title, this.context, this.actions = const [], this.boundary});

  final AppState state;
  final String title;
  final String? context;
  final String? boundary;
  final List<Widget> actions;

  @override
  Widget build(BuildContext ctx) {
    final p = ctx.palette;
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 2, 4, 10),
      child: Row(children: [
        if (state.canGoBack) IconAction(OIcon.back, label: '返回上一处', onPressed: state.back),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Row(children: [
              Flexible(child: Semantics(header: true, child: Text(title, maxLines: 2, overflow: TextOverflow.ellipsis, style: textStyle(p, size: TypeScale.title, weight: FontWeight.w600, height: 1.3)))),
              if (state.content.isSample) ...[const SizedBox(width: 8), const SampleMark()],
            ]),
            if (context != null) Text(context!, maxLines: 1, overflow: TextOverflow.ellipsis, style: textStyle(p, size: 13, color: p.textSecondary, height: 1.4)),
            if (boundary != null) Padding(padding: const EdgeInsets.only(top: 4), child: Tag(boundary!, icon: OIcon.pin, tone: p.warning)),
          ]),
        ),
        for (final a in actions) Padding(padding: const EdgeInsets.only(left: 6), child: a),
      ]),
    );
  }
}

OIcon iconForArtifact(ArtifactKind k) => switch (k) {
      ArtifactKind.article => OIcon.text,
      ArtifactKind.video || ArtifactKind.podcast => OIcon.create,
      _ => OIcon.reader,
    };

OIcon iconForKind(ObjectKind k) => switch (k) {
      ObjectKind.work => OIcon.clients,
      ObjectKind.material => OIcon.research,
      ObjectKind.artifact => OIcon.create,
      ObjectKind.monitor => OIcon.monitor,
      ObjectKind.subject => OIcon.subjects,
      ObjectKind.client => OIcon.clients,
      ObjectKind.release => OIcon.community,
      ObjectKind.note => OIcon.text,
    };

Color workStateColor(Palette p, WorkState s) => switch (s) {
      WorkState.inProgress => p.accentText,
      WorkState.waitingInput => p.warning,
      WorkState.review => p.accentText,
      WorkState.delivered => p.positive,
      WorkState.paused => p.textTertiary,
    };

/// Two-way toggle used on narrow widths to switch between index and desk.
class Segmented<T> extends StatelessWidget {
  const Segmented({super.key, required this.values, required this.selected, required this.label, required this.onChanged});
  final List<T> values;
  final T selected;
  final String Function(T) label;
  final void Function(T) onChanged;

  @override
  Widget build(BuildContext context) {
    final p = context.palette;
    return Container(
      padding: const EdgeInsets.all(3),
      decoration: BoxDecoration(color: p.surfaceSunken, borderRadius: BorderRadius.circular(Radii.control + 2), border: Border.all(color: p.line)),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        for (final v in values)
          Semantics(
            selected: v == selected,
            button: true,
            child: GestureDetector(
              onTap: () => onChanged(v),
              child: MouseRegion(
                cursor: SystemMouseCursors.click,
                child: Container(
                  constraints: const BoxConstraints(minHeight: 32, minWidth: 56),
                  alignment: Alignment.center,
                  padding: const EdgeInsets.symmetric(horizontal: 12),
                  decoration: BoxDecoration(
                    color: v == selected ? p.surface : Colors.transparent,
                    borderRadius: BorderRadius.circular(Radii.control),
                    boxShadow: v == selected ? [BoxShadow(color: p.contactShadow, blurRadius: 3, offset: const Offset(0, 1))] : null,
                  ),
                  child: Text(label(v), style: textStyle(p, size: 13, weight: FontWeight.w600, color: v == selected ? p.text : p.textSecondary, height: 1.2)),
                ),
              ),
            ),
          ),
      ]),
    );
  }
}

/// Small honest line chart: gaps stay gaps, nothing is smoothed over.
class Sparkline extends StatelessWidget {
  const Sparkline(this.values, {super.key, this.height = 64, this.highlightFrom});
  final List<double?> values;
  final double height;
  final int? highlightFrom;

  @override
  Widget build(BuildContext context) => SizedBox(height: height, child: CustomPaint(painter: _SparkPainter(values, context.palette, highlightFrom), size: Size.infinite));
}

class _SparkPainter extends CustomPainter {
  _SparkPainter(this.values, this.p, this.highlightFrom);
  final List<double?> values;
  final Palette p;
  final int? highlightFrom;

  @override
  void paint(Canvas canvas, Size size) {
    final known = values.whereType<double>();
    if (known.isEmpty) return;
    final lo = known.reduce((a, b) => a < b ? a : b) * 0.9;
    final hi = known.reduce((a, b) => a > b ? a : b) * 1.05;
    Offset at(int i, double v) => Offset(i / (values.length - 1) * size.width, size.height - (v - lo) / (hi - lo) * size.height);
    final grid = Paint()..color = p.line;
    for (var k = 0; k <= 2; k++) {
      final y = size.height * k / 2;
      canvas.drawLine(Offset(0, y), Offset(size.width, y), grid);
    }
    final stroke = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2
      ..strokeCap = StrokeCap.round;
    for (var i = 1; i < values.length; i++) {
      final a = values[i - 1], b = values[i];
      if (a == null || b == null) continue;
      stroke.color = highlightFrom != null && i > highlightFrom! ? p.danger : p.accent;
      canvas.drawLine(at(i - 1, a), at(i, b), stroke);
    }
    for (var i = 0; i < values.length; i++) {
      final v = values[i];
      if (v == null) {
        final x = i / (values.length - 1) * size.width;
        canvas.drawLine(Offset(x, size.height * 0.15), Offset(x, size.height), Paint()..color = p.warning.withValues(alpha: 0.6)..strokeWidth = 1);
        continue;
      }
      canvas.drawCircle(at(i, v), 2.6, Paint()..color = p.surface);
      canvas.drawCircle(at(i, v), 2.6, Paint()..color = (highlightFrom != null && i > highlightFrom!) ? p.danger : p.accent..style = PaintingStyle.stroke..strokeWidth = 1.6);
    }
  }

  @override
  bool shouldRepaint(_SparkPainter old) => old.values != values || old.p != p;
}

/// Brief, non-blocking notice anchored to the bottom of the window. Used for
/// actions this sample build shows but cannot carry out without a service.
void showNotice(BuildContext context, String text) {
  final overlay = Overlay.maybeOf(context);
  if (overlay == null) return;
  final p = context.palette;
  late final OverlayEntry entry;
  entry = OverlayEntry(
    builder: (_) => Positioned(
      left: 0,
      right: 0,
      bottom: 28,
      child: IgnorePointer(
        child: Center(
          child: Semantics(
            liveRegion: true,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
              decoration: BoxDecoration(color: p.text, borderRadius: BorderRadius.circular(Radii.pill), boxShadow: [BoxShadow(color: p.liftShadow, blurRadius: 18, offset: const Offset(0, 6))]),
              child: Text(text, style: textStyle(p, size: 13.5, color: p.surface, height: 1.3, weight: FontWeight.w500)),
            ),
          ),
        ),
      ),
    ),
  );
  overlay.insert(entry);
  Future<void>.delayed(const Duration(milliseconds: 2400), entry.remove);
}

const String notConnected = '示例版本未连接服务，此操作只演示位置';
