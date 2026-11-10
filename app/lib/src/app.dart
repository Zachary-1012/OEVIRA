import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'content/content_source.dart';
import 'navigation/destinations.dart';
import 'shell/workspace_shell.dart';
import 'state/app_state.dart';
import 'theme/theme.dart';
import 'theme/tokens.dart';

/// Root of the visual client. Content comes from whatever [ContentSource] is
/// injected; today that is the sample source, later a backend adapter.
class OeviraApp extends StatefulWidget {
  const OeviraApp({super.key, required this.content, this.preferences, this.initialDestination, this.initialAppearance});
  final ContentSource content;
  final SharedPreferences? preferences;
  final Destination? initialDestination;
  final Appearance? initialAppearance;

  @override
  State<OeviraApp> createState() => _OeviraAppState();
}

class _OeviraAppState extends State<OeviraApp> {
  late final AppState _state = AppState(widget.content, preferences: widget.preferences);

  @override
  void initState() {
    super.initState();
    if (widget.initialAppearance != null) _state.setAppearance(widget.initialAppearance!);
    if (widget.initialDestination != null) _state.go(widget.initialDestination!);
  }

  @override
  void dispose() {
    _state.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: _state,
      builder: (context, _) {
        final platformNight = MediaQuery.platformBrightnessOf(context) == Brightness.dark;
        final night = switch (_state.appearance) {
          Appearance.day => false,
          Appearance.night => true,
          Appearance.system => platformNight,
        };
        final palette = night ? Palette.night : Palette.day;
        return PaletteScope(
          palette: palette,
          child: MaterialApp(
            title: '寰意',
            debugShowCheckedModeBanner: false,
            theme: buildTheme(palette),
            home: PaletteScope(palette: palette, child: Material(type: MaterialType.transparency, child: WorkspaceShell(state: _state))),
          ),
        );
      },
    );
  }
}
