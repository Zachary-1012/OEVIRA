import 'package:flutter/widgets.dart';

/// Every visual constant the client uses. DESIGN.md owns the values; widgets
/// read them from here so day/night and density stay one decision.
abstract final class Space {
  static const double xxs = 2;
  static const double xs = 4;
  static const double sm = 8;
  static const double md = 12;
  static const double lg = 16;
  static const double xl = 24;
  static const double xxl = 32;

  /// Dock gap and outer margin from DESIGN "停靠预设的可执行初值".
  static const double dockGap = 12;
  static const double outerMargin = 16;
}

abstract final class Dock {
  static const double navDefault = 240;
  static const double navMin = 200;
  static const double navMax = 320;
  static const double toolDefault = 320;
  static const double toolMin = 280;
  static const double toolMax = 440;
  static const double contentMin = 560;
  static const double titleBar = 52;
}

abstract final class Breakpoints {
  static const double wide = 1280;
  static const double medium = 768;
}

abstract final class Radii {
  static const double control = 8;
  static const double surface = 12;
  static const double pane = 14;
  static const double object = 10;
  static const double pill = 999;
}

abstract final class Targets {
  /// Regular click area and important touch action from DESIGN 图标比例.
  static const double click = 40;
  static const double touch = 44;
  static const double minimum = 24;
}

abstract final class IconSizes {
  static const double regular = 20;
  static const double compact = 16;
  static const double stroke = 1.6;
  static const double character = 28;
}

abstract final class Motion {
  static const Duration short = Duration(milliseconds: 120);
  static const Duration medium = Duration(milliseconds: 200);
  static const Duration long = Duration(milliseconds: 280);
  static const Curve standard = Cubic(0.2, 0, 0, 1);

  /// Free slide after release: v(t) = v0 * exp(-lambda * t).
  static const double slideLambda = 8;

  /// Pointer travel before a grip press becomes a spatial drag.
  static const double mouseSlop = 6;
  static const double touchSlop = 10;
}

abstract final class TypeScale {
  static const String family = 'NotoSansSC';
  static const List<String> fallback = ['NotoSansKR', 'NotoColorEmoji'];

  static const double body = 16;
  static const double bodyHeight = 1.55;
  static const double dense = 14;
  static const double denseHeight = 1.45;
  static const double caption = 12.5;
  static const double title = 20;
  static const double headline = 26;
  static const double section = 13;
}

/// Semantic colours for one appearance. Day and night are the only palettes;
/// user photo backgrounds would be a separate preference.
@immutable
class Palette {
  const Palette({
    required this.brightness,
    required this.space,
    required this.spaceLine,
    required this.chrome,
    required this.chromeLine,
    required this.surface,
    required this.surfaceRaised,
    required this.surfaceSunken,
    required this.line,
    required this.lineStrong,
    required this.text,
    required this.textSecondary,
    required this.textTertiary,
    required this.accent,
    required this.accentText,
    required this.accentSoft,
    required this.onAccent,
    required this.focus,
    required this.positive,
    required this.warning,
    required this.danger,
    required this.dangerSoft,
    required this.warningSoft,
    required this.contactShadow,
    required this.liftShadow,
    required this.objectEdge,
    required this.paper,
    required this.paperBack,
  });

  final Brightness brightness;

  /// The workspace floor behind panes and objects.
  final Color space;
  final Color spaceLine;

  /// Translucent navigation / short tool layers. Never under body text.
  final Color chrome;
  final Color chromeLine;

  /// Stable solid content surfaces.
  final Color surface;
  final Color surfaceRaised;
  final Color surfaceSunken;
  final Color line;
  final Color lineStrong;
  final Color text;
  final Color textSecondary;
  final Color textTertiary;
  final Color accent;
  final Color accentText;
  final Color accentSoft;
  final Color onAccent;
  final Color focus;
  final Color positive;
  final Color warning;
  final Color danger;
  final Color dangerSoft;
  final Color warningSoft;
  final Color contactShadow;
  final Color liftShadow;
  final Color objectEdge;
  final Color paper;
  final Color paperBack;

  bool get isNight => brightness == Brightness.dark;

  static const day = Palette(
    brightness: Brightness.light,
    space: Color(0xFFE9EBEF),
    spaceLine: Color(0xFFD9DCE2),
    chrome: Color(0xD9F7F8FA),
    chromeLine: Color(0x1F1B2230),
    surface: Color(0xFFFFFFFF),
    surfaceRaised: Color(0xFFFFFFFF),
    surfaceSunken: Color(0xFFF3F4F7),
    line: Color(0xFFE1E4EA),
    lineStrong: Color(0xFF8C93A1),
    text: Color(0xFF15181E),
    textSecondary: Color(0xFF4A515E),
    textTertiary: Color(0xFF656C79),
    accent: Color(0xFF1B5FD1),
    accentText: Color(0xFF1650B5),
    accentSoft: Color(0xFFE3ECFA),
    onAccent: Color(0xFFFFFFFF),
    focus: Color(0xFF1B5FD1),
    positive: Color(0xFF1C7A4E),
    warning: Color(0xFF8A5A00),
    danger: Color(0xFFB4232F),
    dangerSoft: Color(0xFFFBE9EA),
    warningSoft: Color(0xFFFBF1DD),
    contactShadow: Color(0x2E0E1320),
    liftShadow: Color(0x330E1320),
    objectEdge: Color(0xFFCDD1D9),
    paper: Color(0xFFFFFFFF),
    paperBack: Color(0xFFF6F7F9),
  );

  static const night = Palette(
    brightness: Brightness.dark,
    space: Color(0xFF0E1014),
    spaceLine: Color(0xFF1C2027),
    chrome: Color(0xCC161A20),
    chromeLine: Color(0x24FFFFFF),
    surface: Color(0xFF181B21),
    surfaceRaised: Color(0xFF1F232A),
    surfaceSunken: Color(0xFF13161B),
    line: Color(0xFF2B3039),
    lineStrong: Color(0xFF7D8594),
    text: Color(0xFFECEEF2),
    textSecondary: Color(0xFFB4BAC5),
    textTertiary: Color(0xFF959CA9),
    accent: Color(0xFF6FA2FF),
    accentText: Color(0xFF8DB5FF),
    accentSoft: Color(0xFF1C2A40),
    onAccent: Color(0xFF0E1236),
    focus: Color(0xFF8DB5FF),
    positive: Color(0xFF5FCB94),
    warning: Color(0xFFE5B65A),
    danger: Color(0xFFFF7B83),
    dangerSoft: Color(0xFF3A1E22),
    warningSoft: Color(0xFF3A2F18),
    contactShadow: Color(0x80000000),
    liftShadow: Color(0x99000000),
    objectEdge: Color(0xFF353B46),
    paper: Color(0xFF1D2128),
    paperBack: Color(0xFF191C22),
  );
}
