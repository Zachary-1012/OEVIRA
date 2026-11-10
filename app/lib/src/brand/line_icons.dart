import 'dart:math' as math;

import 'package:flutter/widgets.dart';

import '../theme/tokens.dart';

/// The product's line icon set. Each glyph is drawn on a 24-unit canvas with
/// the graphic kept inside 2..22 so the regular size has a 20-unit body.
enum OIcon {
  workbench, clients, subjects, monitor, research, create, library, community,
  search, browser, ai, plus, open, sun, moon, navPanel, toolPanel, grip, flip,
  stow, pin, front, close, chevron, chevronDown, link, text, more, refresh,
  settings, help, apps, check, alert, send, play, pause, reader, back, minus, history, focus,
}

class LineIcon extends StatelessWidget {
  const LineIcon(this.icon, {super.key, this.size = IconSizes.regular, this.color, this.accent, this.semanticLabel});

  final OIcon icon;
  final double size;
  final Color? color;

  /// Optional body colour for the local fill some glyphs allow.
  final Color? accent;
  final String? semanticLabel;

  @override
  Widget build(BuildContext context) {
    final colour = color ?? DefaultTextStyle.of(context).style.color ?? const Color(0xFF000000);
    final glyph = CustomPaint(
      size: Size.square(size),
      painter: _LineIconPainter(icon, colour, accent),
    );
    if (semanticLabel == null) return ExcludeSemantics(child: glyph);
    return Semantics(label: semanticLabel, child: glyph);
  }
}

class _LineIconPainter extends CustomPainter {
  _LineIconPainter(this.icon, this.color, this.accent);

  final OIcon icon;
  final Color color;
  final Color? accent;

  @override
  void paint(Canvas canvas, Size size) {
    final unit = size.width / 24;
    canvas.scale(unit);
    final stroke = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = IconSizes.stroke * (size.width >= 20 ? 1 : 1.12)
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final fill = Paint()..color = accent ?? color.withValues(alpha: 0.16);
    final dot = Paint()..color = color;
    _draw(canvas, stroke, fill, dot);
  }

  void _draw(Canvas c, Paint s, Paint f, Paint d) {
    RRect rr(double l, double t, double r, double b, double radius) =>
        RRect.fromLTRBR(l, t, r, b, Radius.circular(radius));
    Path line(List<Offset> points) {
      final p = Path()..moveTo(points.first.dx, points.first.dy);
      for (final point in points.skip(1)) {
        p.lineTo(point.dx, point.dy);
      }
      return p;
    }

    switch (icon) {
      case OIcon.workbench:
        c.drawRRect(rr(3, 4, 21, 15, 2.5), f);
        c.drawRRect(rr(3, 4, 21, 15, 2.5), s);
        c.drawPath(line(const [Offset(8, 20), Offset(16, 20)]), s);
        c.drawPath(line(const [Offset(12, 15), Offset(12, 20)]), s);
      case OIcon.clients:
        c.drawRRect(rr(3, 7, 21, 19, 2.5), s);
        c.drawPath(line(const [Offset(9, 7), Offset(9, 5), Offset(15, 5), Offset(15, 7)]), s);
        c.drawPath(line(const [Offset(3, 12.5), Offset(21, 12.5)]), s);
        c.drawCircle(const Offset(12, 12.5), 1.3, d);
      case OIcon.subjects:
        c.drawPath(line(const [Offset(4, 20), Offset(4, 8), Offset(11, 4), Offset(11, 20)]), s);
        c.drawPath(line(const [Offset(11, 10), Offset(20, 10), Offset(20, 20)]), s);
        c.drawPath(line(const [Offset(2.5, 20), Offset(21.5, 20)]), s);
        for (final y in const [11.0, 15.0]) {
          c.drawPath(line([Offset(7, y), Offset(8, y)]), s);
          c.drawPath(line([Offset(15, y + 1), Offset(16, y + 1)]), s);
        }
      case OIcon.monitor:
        c.drawCircle(const Offset(12, 12), 8.5, s);
        c.drawArc(Rect.fromCircle(center: const Offset(12, 12), radius: 4.5), -math.pi * 0.9, math.pi * 1.2, false, s);
        c.drawPath(line(const [Offset(12, 12), Offset(17.5, 6.5)]), s);
        c.drawCircle(const Offset(12, 12), 1.4, d);
      case OIcon.research:
        final page = Path()
          ..moveTo(12, 6.5)
          ..cubicTo(9.5, 5, 6.5, 4.6, 3, 5)
          ..lineTo(3, 18.5)
          ..cubicTo(6.5, 18.1, 9.5, 18.5, 12, 20)
          ..cubicTo(14.5, 18.5, 17.5, 18.1, 21, 18.5)
          ..lineTo(21, 5)
          ..cubicTo(17.5, 4.6, 14.5, 5, 12, 6.5)
          ..close();
        c.drawPath(page, f);
        c.drawPath(page, s);
        c.drawPath(line(const [Offset(12, 6.5), Offset(12, 20)]), s);
      case OIcon.create:
        c.drawPath(line(const [Offset(4, 20), Offset(5, 15.5), Offset(15.5, 5), Offset(19, 8.5), Offset(8.5, 19), Offset(4, 20)]), s);
        c.drawPath(line(const [Offset(13, 7.5), Offset(16.5, 11)]), s);
        c.drawPath(line(const [Offset(13, 20), Offset(20, 20)]), s);
      case OIcon.library:
        c.drawRRect(rr(3, 4, 21, 9, 2), s);
        c.drawPath(line(const [Offset(4.5, 9), Offset(4.5, 18.5), Offset(19.5, 18.5), Offset(19.5, 9)]), s);
        c.drawPath(line(const [Offset(10, 13), Offset(14, 13)]), s);
      case OIcon.community:
        c.drawCircle(const Offset(8.5, 9), 3, s);
        c.drawCircle(const Offset(16.5, 10), 2.4, s);
        c.drawArc(const Rect.fromLTRB(3, 14, 14, 25), math.pi * 1.1, math.pi * 0.8, false, s);
        c.drawArc(const Rect.fromLTRB(12.5, 14.5, 21.5, 23.5), math.pi * 1.15, math.pi * 0.75, false, s);
      case OIcon.search:
        c.drawCircle(const Offset(10.5, 10.5), 6.5, s);
        c.drawPath(line(const [Offset(15.3, 15.3), Offset(20.5, 20.5)]), s);
      case OIcon.browser:
        c.drawCircle(const Offset(12, 12), 8.5, s);
        c.drawOval(const Rect.fromLTRB(8, 3.5, 16, 20.5), s);
        c.drawPath(line(const [Offset(3.5, 12), Offset(20.5, 12)]), s);
      case OIcon.ai:
        final bubble = Path()
          ..moveTo(5, 5)
          ..lineTo(19, 5)
          ..quadraticBezierTo(21, 5, 21, 7)
          ..lineTo(21, 15)
          ..quadraticBezierTo(21, 17, 19, 17)
          ..lineTo(11, 17)
          ..lineTo(6.5, 20.5)
          ..lineTo(6.5, 17)
          ..lineTo(5, 17)
          ..quadraticBezierTo(3, 17, 3, 15)
          ..lineTo(3, 7)
          ..quadraticBezierTo(3, 5, 5, 5)
          ..close();
        c.drawPath(bubble, s);
        c.drawArc(const Rect.fromLTRB(8, 7.5, 16, 14.5), math.pi * 0.15, math.pi * 1.5, false, s);
      case OIcon.plus:
        c.drawPath(line(const [Offset(12, 5), Offset(12, 19)]), s);
        c.drawPath(line(const [Offset(5, 12), Offset(19, 12)]), s);
      case OIcon.minus:
        c.drawPath(line(const [Offset(5, 12), Offset(19, 12)]), s);
      case OIcon.open:
        c.drawPath(line(const [Offset(3, 18.5), Offset(3, 6), Offset(9, 6), Offset(11, 8), Offset(19, 8), Offset(19, 10.5)]), s);
        c.drawPath(line(const [Offset(3, 18.5), Offset(6.5, 11), Offset(21.5, 11), Offset(18, 18.5), Offset(3, 18.5)]), s);
      case OIcon.sun:
        c.drawCircle(const Offset(12, 12), 4, s);
        for (var i = 0; i < 8; i++) {
          final a = i * math.pi / 4;
          c.drawPath(line([Offset(12 + math.cos(a) * 7, 12 + math.sin(a) * 7), Offset(12 + math.cos(a) * 8.8, 12 + math.sin(a) * 8.8)]), s);
        }
      case OIcon.moon:
        final moon = Path()
          ..moveTo(18.5, 14.5)
          ..arcToPoint(const Offset(9.5, 5.5), radius: const Radius.circular(7.5), clockwise: false)
          ..arcToPoint(const Offset(18.5, 14.5), radius: const Radius.circular(8.5), clockwise: false);
        c.drawPath(moon, s);
      case OIcon.navPanel:
        c.drawRRect(rr(3, 4.5, 21, 19.5, 2.5), s);
        c.drawPath(line(const [Offset(9, 4.5), Offset(9, 19.5)]), s);
      case OIcon.toolPanel:
        c.drawRRect(rr(3, 4.5, 21, 19.5, 2.5), s);
        c.drawPath(line(const [Offset(15, 4.5), Offset(15, 19.5)]), s);
      case OIcon.grip:
        for (final x in const [9.5, 14.5]) {
          for (final y in const [7.0, 12.0, 17.0]) {
            c.drawCircle(Offset(x, y), 1.35, d);
          }
        }
      case OIcon.flip:
        c.drawArc(const Rect.fromLTRB(4, 5, 20, 19), math.pi * 1.05, math.pi * 0.95, false, s);
        c.drawPath(line(const [Offset(17.2, 4.6), Offset(19.6, 8.2), Offset(15.8, 9.4)]), s);
        c.drawArc(const Rect.fromLTRB(4, 5, 20, 19), math.pi * 0.05, math.pi * 0.95, false, s);
        c.drawPath(line(const [Offset(6.8, 19.4), Offset(4.4, 15.8), Offset(8.2, 14.6)]), s);
      case OIcon.stow:
        c.drawPath(line(const [Offset(3, 13), Offset(7.5, 13), Offset(9, 15.5), Offset(15, 15.5), Offset(16.5, 13), Offset(21, 13)]), s);
        c.drawRRect(rr(3, 13, 21, 20, 2.5), s);
        c.drawPath(line(const [Offset(12, 3.5), Offset(12, 10)]), s);
        c.drawPath(line(const [Offset(9, 7.5), Offset(12, 10.5), Offset(15, 7.5)]), s);
      case OIcon.pin:
        c.drawPath(line(const [Offset(9, 3.5), Offset(15, 3.5)]), s);
        c.drawPath(line(const [Offset(10, 3.5), Offset(10, 9), Offset(7, 13), Offset(17, 13), Offset(14, 9), Offset(14, 3.5)]), s);
        c.drawPath(line(const [Offset(12, 13), Offset(12, 20.5)]), s);
      case OIcon.front:
        c.drawRRect(rr(3, 8, 14, 19, 2), s);
        c.drawRRect(rr(9, 4, 21, 15, 2), f);
        c.drawRRect(rr(9, 4, 21, 15, 2), s);
      case OIcon.close:
        c.drawPath(line(const [Offset(6, 6), Offset(18, 18)]), s);
        c.drawPath(line(const [Offset(18, 6), Offset(6, 18)]), s);
      case OIcon.chevron:
        c.drawPath(line(const [Offset(9.5, 6), Offset(15.5, 12), Offset(9.5, 18)]), s);
      case OIcon.chevronDown:
        c.drawPath(line(const [Offset(6, 9.5), Offset(12, 15.5), Offset(18, 9.5)]), s);
      case OIcon.back:
        c.drawPath(line(const [Offset(14.5, 6), Offset(8.5, 12), Offset(14.5, 18)]), s);
      case OIcon.link:
        c.drawRRect(rr(3, 8.5, 13, 15.5, 3.5), s);
        c.drawRRect(rr(11, 8.5, 21, 15.5, 3.5), s);
      case OIcon.text:
        c.drawPath(line(const [Offset(5, 6), Offset(19, 6)]), s);
        c.drawPath(line(const [Offset(12, 6), Offset(12, 19)]), s);
        c.drawPath(line(const [Offset(9, 19), Offset(15, 19)]), s);
      case OIcon.more:
        for (final x in const [6.0, 12.0, 18.0]) {
          c.drawCircle(Offset(x, 12), 1.5, d);
        }
      case OIcon.refresh:
        c.drawArc(const Rect.fromLTRB(4.5, 4.5, 19.5, 19.5), -math.pi * 0.3, math.pi * 1.65, false, s);
        c.drawPath(line(const [Offset(19.5, 4), Offset(19.2, 8.4), Offset(14.8, 8.1)]), s);
      case OIcon.settings:
        c.drawCircle(const Offset(12, 12), 3, s);
        for (var i = 0; i < 6; i++) {
          final a = i * math.pi / 3;
          c.drawPath(line([Offset(12 + math.cos(a) * 6.2, 12 + math.sin(a) * 6.2), Offset(12 + math.cos(a) * 8.8, 12 + math.sin(a) * 8.8)]), s);
        }
        c.drawCircle(const Offset(12, 12), 6.2, s);
      case OIcon.help:
        c.drawCircle(const Offset(12, 12), 8.5, s);
        final q = Path()
          ..moveTo(9.5, 9.5)
          ..arcToPoint(const Offset(14.5, 9.8), radius: const Radius.circular(2.6))
          ..cubicTo(14.5, 11.8, 12, 11.8, 12, 13.8);
        c.drawPath(q, s);
        c.drawCircle(const Offset(12, 16.8), 1.1, d);
      case OIcon.apps:
        for (final o in const [Offset(4, 4), Offset(13.5, 4), Offset(4, 13.5), Offset(13.5, 13.5)]) {
          c.drawRRect(rr(o.dx, o.dy, o.dx + 6.5, o.dy + 6.5, 1.8), s);
        }
      case OIcon.check:
        c.drawPath(line(const [Offset(5, 12.5), Offset(10, 17.5), Offset(19, 7)]), s);
      case OIcon.alert:
        c.drawPath(line(const [Offset(12, 3.8), Offset(21, 19.5), Offset(3, 19.5), Offset(12, 3.8)]), s);
        c.drawPath(line(const [Offset(12, 9.5), Offset(12, 13.5)]), s);
        c.drawCircle(const Offset(12, 16.5), 1.1, d);
      case OIcon.send:
        c.drawPath(line(const [Offset(12, 19), Offset(12, 5.5)]), s);
        c.drawPath(line(const [Offset(6.5, 11), Offset(12, 5.5), Offset(17.5, 11)]), s);
      case OIcon.play:
        c.drawPath(line(const [Offset(8, 5.5), Offset(18.5, 12), Offset(8, 18.5), Offset(8, 5.5)]), s);
      case OIcon.pause:
        c.drawPath(line(const [Offset(9, 6), Offset(9, 18)]), s);
        c.drawPath(line(const [Offset(15, 6), Offset(15, 18)]), s);
      case OIcon.reader:
        c.drawRRect(rr(5, 3, 19, 21, 2.5), s);
        for (final y in const [8.0, 12.0, 16.0]) {
          c.drawPath(line([Offset(8.5, y), Offset(y == 16 ? 13 : 15.5, y)]), s);
        }
      case OIcon.history:
        c.drawArc(const Rect.fromLTRB(4, 4, 20, 20), math.pi * 0.85, math.pi * 1.75, false, s);
        c.drawPath(line(const [Offset(3.2, 9.5), Offset(5, 13), Offset(8.2, 10.8)]), s);
        c.drawPath(line(const [Offset(12, 8), Offset(12, 12.5), Offset(15, 14)]), s);
      case OIcon.focus:
        for (final corner in const [
          [Offset(4, 9), Offset(4, 4), Offset(9, 4)],
          [Offset(15, 4), Offset(20, 4), Offset(20, 9)],
          [Offset(20, 15), Offset(20, 20), Offset(15, 20)],
          [Offset(9, 20), Offset(4, 20), Offset(4, 15)],
        ]) {
          c.drawPath(line(corner), s);
        }
    }
  }

  @override
  bool shouldRepaint(_LineIconPainter old) => old.icon != icon || old.color != color || old.accent != accent;
}
