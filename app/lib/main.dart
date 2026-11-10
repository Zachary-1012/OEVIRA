import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/semantics.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'src/app.dart';
import 'src/content/sample/sample_content.dart';
import 'src/navigation/destinations.dart';
import 'src/state/app_state.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final prefs = await SharedPreferences.getInstance();
  // Optional launch parameters (web: ?d=research&theme=night&fresh=1) so a
  // reviewer can open any surface directly. They never change content.
  final q = Uri.base.queryParameters;
  if (q['fresh'] == '1') await prefs.clear();
  final destination = Destination.values.where((d) => d.name == q['d']).firstOrNull;
  final appearance = switch (q['theme']) {
    'night' => Appearance.night,
    'day' => Appearance.day,
    _ => null,
  };
  runApp(OeviraApp(content: const SampleContentSource(), preferences: prefs, initialDestination: destination, initialAppearance: appearance));
  // DESIGN/FRONTEND-EXECUTION: the web build bridges the semantics tree to
  // accessible DOM explicitly instead of waiting for the hidden opt-in button.
  if (kIsWeb) SemanticsBinding.instance.ensureSemantics();
}
