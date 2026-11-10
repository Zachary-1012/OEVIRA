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
    final stroke = size.width <= 18 ? 2.1 : 1.8;
    final outer = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.round;
    final inner = Paint()
      ..color = accent
      ..style = PaintingStyle.stroke
      ..strokeWidth = stroke
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    // Outer arc: opens at upper right, leaving a 2-unit gap.
    canvas.drawArc(const Rect.fromLTRB(2.5, 2.5, 21.5, 21.5), -math.pi * 0.18, -math.pi * 1.48, false, outer);
    // Second arc continues the ring and folds inward toward the centre.
    final fold = Path()
      ..moveTo(19.9, 7.9)
      ..arcToPoint(const Offset(12, 21.5), radius: const Radius.circular(9.5))
      ..moveTo(19.9, 7.9)
      ..quadraticBezierTo(15.6, 9.2, 13.6, 12.6)
      ..quadraticBezierTo(12.6, 14.4, 14.6, 15.2);
    canvas.drawPath(fold, inner);
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
