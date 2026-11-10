import 'package:flutter/widgets.dart';

import '../navigation/destinations.dart';
import '../state/app_state.dart';
import 'browser.dart';
import 'clients.dart';
import 'community.dart';
import 'creation.dart';
import 'library.dart';
import 'monitoring.dart';
import 'research.dart';
import 'subjects.dart';
import 'workbench.dart';

typedef SurfaceBuilder = Widget Function(AppState state, bool narrow);

/// One registry from destination to surface; adding a surface is one line.
final Map<Destination, SurfaceBuilder> surfaceRegistry = {
  Destination.workbench: (s, n) => WorkbenchSurface(state: s, narrow: n),
  Destination.clients: (s, n) => ClientsSurface(state: s, narrow: n),
  Destination.subjects: (s, n) => SubjectsSurface(state: s, narrow: n),
  Destination.monitoring: (s, n) => MonitoringSurface(state: s, narrow: n),
  Destination.research: (s, n) => ResearchSurface(state: s, narrow: n),
  Destination.creation: (s, n) => CreationSurface(state: s, narrow: n),
  Destination.library: (s, n) => LibrarySurface(state: s, narrow: n),
  Destination.community: (s, n) => CommunitySurface(state: s, narrow: n),
  Destination.browser: (s, n) => BrowserSurface(state: s, narrow: n),
};

class Surfaces extends StatelessWidget {
  const Surfaces({super.key, required this.state, required this.narrow});
  final AppState state;
  final bool narrow;

  @override
  Widget build(BuildContext context) => KeyedSubtree(key: ValueKey(state.destination), child: surfaceRegistry[state.destination]!(state, narrow));
}
