import 'package:flutter/material.dart';

import 'tokens.dart';

/// Gives widgets the active palette without threading it through constructors.
class PaletteScope extends InheritedWidget {
  const PaletteScope({super.key, required this.palette, required super.child});

  final Palette palette;

  static Palette of(BuildContext context) =>
      context.dependOnInheritedWidgetOfExactType<PaletteScope>()?.palette ?? Palette.day;

  @override
  bool updateShouldNotify(PaletteScope oldWidget) => oldWidget.palette != palette;
}

extension PaletteContext on BuildContext {
  Palette get palette => PaletteScope.of(this);
}

TextStyle textStyle(Palette p, {double size = TypeScale.body, double? height, FontWeight weight = FontWeight.w400, Color? color}) {
  final lineHeight = height ?? (size <= TypeScale.dense ? TypeScale.denseHeight : TypeScale.bodyHeight);
  return TextStyle(
    fontFamily: TypeScale.family,
    fontFamilyFallback: TypeScale.fallback,
    fontSize: size,
    height: lineHeight,
    fontWeight: weight,
    color: color ?? p.text,
    letterSpacing: 0,
  );
}

ThemeData buildTheme(Palette p) {
  final base = textStyle(p);
  final scheme = ColorScheme(
    brightness: p.brightness,
    primary: p.accent,
    onPrimary: p.onAccent,
    secondary: p.accent,
    onSecondary: p.onAccent,
    error: p.danger,
    onError: p.onAccent,
    surface: p.surface,
    onSurface: p.text,
  );
  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    brightness: p.brightness,
    fontFamily: TypeScale.family,
    fontFamilyFallback: TypeScale.fallback,
    scaffoldBackgroundColor: p.space,
    splashFactory: NoSplash.splashFactory,
    highlightColor: Colors.transparent,
    hoverColor: p.text.withValues(alpha: 0.05),
    focusColor: p.focus.withValues(alpha: 0.16),
    textSelectionTheme: TextSelectionThemeData(
      cursorColor: p.accent,
      selectionColor: p.accent.withValues(alpha: 0.28),
      selectionHandleColor: p.accent,
    ),
    textTheme: TextTheme(
      bodyLarge: base,
      bodyMedium: textStyle(p, size: TypeScale.dense),
      bodySmall: textStyle(p, size: TypeScale.caption, color: p.textSecondary),
      titleMedium: textStyle(p, size: TypeScale.body, weight: FontWeight.w600),
      titleLarge: textStyle(p, size: TypeScale.title, weight: FontWeight.w600, height: 1.35),
      labelLarge: textStyle(p, size: TypeScale.dense, weight: FontWeight.w500),
    ),
    tooltipTheme: TooltipThemeData(
      waitDuration: const Duration(milliseconds: 500),
      textStyle: textStyle(p, size: TypeScale.caption, color: p.isNight ? p.space : Colors.white, height: 1.3),
      decoration: BoxDecoration(color: p.isNight ? p.text : const Color(0xFF1B1F27), borderRadius: BorderRadius.circular(6)),
    ),
    inputDecorationTheme: InputDecorationTheme(
      isDense: true,
      filled: true,
      fillColor: p.surfaceSunken,
      hintStyle: textStyle(p, size: TypeScale.dense, color: p.textTertiary),
      contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      border: OutlineInputBorder(borderRadius: BorderRadius.circular(Radii.control), borderSide: BorderSide(color: p.line)),
      enabledBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(Radii.control), borderSide: BorderSide(color: p.line)),
      focusedBorder: OutlineInputBorder(borderRadius: BorderRadius.circular(Radii.control), borderSide: BorderSide(color: p.focus, width: 1.6)),
    ),
    scrollbarTheme: ScrollbarThemeData(
      thickness: WidgetStateProperty.all(6),
      radius: const Radius.circular(3),
      thumbColor: WidgetStateProperty.all(p.lineStrong.withValues(alpha: 0.45)),
    ),
    dividerColor: p.line,
  );
}
