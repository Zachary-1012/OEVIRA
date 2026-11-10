import 'dart:math' as math;

import 'package:flutter/widgets.dart';

import '../theme/tokens.dart';

/// 寰意 mark: two related open arcs forming a ring field, with one inward fold
/// carrying intent into the space. Drawn on a 24-unit grid, body 19 units.
class OeviraLogo extends StatelessWidget {
  const OeviraLogo({super.key, this.size = 22, this.color, this.accent});

  final double size;
  final Color? color;
  final Color? accent;

  @override
  Widget build(BuildContext context) {
    final c = color ?? DefaultTextStyle.of(context).style.color ?? const Color(0xFF000000);
    return Semantics(
      label: '寰意',
      child: CustomPaint(size: Size.square(size), painter: _LogoPainter(c, accent ?? c)),
    );
  }
}

class _LogoPainter extends CustomPainter {
  _LogoPainter(this.color, this.accent);
  final Color color;
  final Color accent;

  @override
  void paint(Canvas canvas, Size size) {
    canvas.scale(size.width / 24);
    // Small sizes get a heavier line so the two gaps and the fold survive
    // at 16px in one colour; the geometry stays the same at every size.
    final stroke = size.width <= 18 ? 2.6 : (size.width <= 24 ? 2.25 : 2.0);
    Paint pen(Color c) => Paint()
      ..color = c
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    const c = Offset(12, 12);
    const r = 8.8;
    double rad(double deg) => deg * math.pi / 180;
    final ring = Rect.fromCircle(center: c, radius: r);
    // Two related open arcs on one ring (body ≈ 19.6 of 24 units). The gaps
    // sit at upper right and lower left, each wider than 3 units.
    canvas.drawArc(ring, rad(-30), rad(170), false, pen(color)); // right/lower arc
    canvas.drawArc(ring, rad(165), rad(105), false, pen(color)); // left/upper arc
    // Inward fold: the upper arc's end turns in toward the centre.
    final start = c + Offset(r * math.cos(rad(270)), r * math.sin(rad(270)));
    final fold = Path()
      ..moveTo(start.dx, start.dy)
      ..quadraticBezierTo(16.6, 6.4, 13.2, 11.6);
    canvas.drawPath(fold, pen(accent));
  }

  @override
  bool shouldRepaint(_LogoPainter old) => old.color != color || old.accent != accent;
}

/// The five optional non-plant companions. Identity comes from outline and pose.
enum Companion {
  xun('寻', '发现'),
  heng('衡', '判断'),
  zhi('织', '构想'),
  yue('跃', '行动'),
  xu('续', '继续');

  const Companion(this.label, this.meaning);
  final String label;
  final String meaning;
}

class CompanionMark extends StatelessWidget {
  const CompanionMark(this.companion, {super.key, this.size = IconSizes.character, this.color, this.accent});

  final Companion companion;
  final double size;
  final Color? color;
  final Color? accent;

  @override
  Widget build(BuildContext context) {
    final c = color ?? DefaultTextStyle.of(context).style.color ?? const Color(0xFF000000);
    return ExcludeSemantics(
      child: CustomPaint(size: Size.square(size), painter: _CompanionPainter(companion, c, accent ?? c.withValues(alpha: 0.18))),
    );
  }
}

class _CompanionPainter extends CustomPainter {
  _CompanionPainter(this.companion, this.color, this.fill);
  final Companion companion;
  final Color color;
  final Color fill;

  @override
  void paint(Canvas canvas, Size size) {
    canvas.scale(size.width / 32);
    final s = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.8
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final f = Paint()..color = fill;
    final eye = Paint()..color = color;
    switch (companion) {
      case Companion.xun:
        canvas.drawCircle(const Offset(15, 17), 9, f);
        canvas.drawArc(const Rect.fromLTRB(6, 8, 24, 26), math.pi * 0.35, math.pi * 1.55, false, s);
        canvas.drawCircle(const Offset(24.5, 8.5), 3, s);
        canvas.drawCircle(const Offset(12.5, 16), 1.2, eye);
        canvas.drawCircle(const Offset(17.5, 16), 1.2, eye);
      case Companion.heng:
        final p = Path()
          ..moveTo(5, 12)
          ..lineTo(11, 8)
          ..lineTo(16, 12)
          ..lineTo(21, 8)
          ..lineTo(27, 12)
          ..moveTo(5, 21)
          ..lineTo(11, 25)
          ..lineTo(16, 21)
          ..lineTo(21, 25)
          ..lineTo(27, 21);
        canvas.drawRRect(RRect.fromLTRBR(7, 12, 25, 21, const Radius.circular(4)), f);
        canvas.drawPath(p, s);
        canvas.drawCircle(const Offset(13, 16.5), 1.2, eye);
        canvas.drawCircle(const Offset(19, 16.5), 1.2, eye);
      case Companion.zhi:
        canvas.drawOval(const Rect.fromLTRB(5, 9, 21, 25), f);
        canvas.drawArc(const Rect.fromLTRB(5, 9, 21, 25), math.pi * 0.1, math.pi * 1.6, false, s);
        canvas.drawArc(const Rect.fromLTRB(11, 7, 27, 23), math.pi * 1.1, math.pi * 1.6, false, s);
        canvas.drawCircle(const Offset(14, 16), 1.2, eye);
        canvas.drawCircle(const Offset(19, 15), 1.2, eye);
      case Companion.yue:
        final p = Path()
          ..moveTo(5, 25)
          ..cubicTo(9, 25, 11, 18, 15, 18)
          ..cubicTo(21, 18, 21, 27, 16, 26)
          ..cubicTo(11, 25, 15, 10, 26, 6);
        canvas.drawCircle(const Offset(16, 21), 5, f);
        canvas.drawPath(p, s);
        canvas.drawPath(Path()..moveTo(22, 5.2)..lineTo(26, 6)..lineTo(24.8, 9.8), s);
        canvas.drawCircle(const Offset(14.5, 21.5), 1.1, eye);
        canvas.drawCircle(const Offset(18, 21.5), 1.1, eye);
      case Companion.xu:
        canvas.drawCircle(const Offset(11.5, 17), 6.5, f);
        canvas.drawArc(const Rect.fromLTRB(5, 10.5, 18, 23.5), math.pi * 0.25, math.pi * 1.6, false, s);
        canvas.drawArc(const Rect.fromLTRB(14, 8.5, 27, 21.5), math.pi * 1.25, math.pi * 1.6, false, s);
        canvas.drawCircle(const Offset(10, 16.5), 1.1, eye);
        canvas.drawCircle(const Offset(13.5, 16.5), 1.1, eye);
    }
  }

  @override
  bool shouldRepaint(_CompanionPainter old) => old.companion != companion || old.color != color || old.fill != fill;
}
