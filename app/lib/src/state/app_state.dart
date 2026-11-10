import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../content/content_source.dart';
import '../content/models.dart';
import '../navigation/destinations.dart';
import '../scene/scene_model.dart';
import '../theme/tokens.dart';

enum Appearance { system, day, night }

enum ToolTab { ai, inspector }

enum ShellLayer { none, search, create }

/// Navigation, dock and appearance state for one window. Business content
/// comes from [ContentSource]; this object only holds the user's view of it.
class AppState extends ChangeNotifier {
  AppState(this.content, {SharedPreferences? preferences}) : _prefs = preferences {
    _restore();
  }

  final ContentSource content;
  final SharedPreferences? _prefs;

  Destination _destination = Destination.workbench;
  Appearance appearance = Appearance.system;
  double navWidth = Dock.navDefault;
  double toolWidth = Dock.toolDefault;
  bool navOpen = true;
  bool toolOpen = true;
  bool focusMode = false;
  ToolTab toolTab = ToolTab.ai;
  ShellLayer overlay = ShellLayer.none;
  final Map<Destination, ObjectRef?> _selection = {};
  final Map<String, List<ChatMessage>> _localMessages = {};
  final Map<String, String> drafts = {};
  final Map<String, SceneController> _scenes = {};
  final List<Destination> _history = [];

  /// Citations the user dropped into the research draft in this session.
  final List<String> researchCitations = [];

  Destination get destination => _destination;
  ObjectRef? get selection => _selection[_destination];

  void go(Destination d) {
    if (d == _destination) return;
    _history.add(_destination);
    _destination = d;
    overlay = ShellLayer.none;
    navDrawer = false;
    notifyListeners();
  }

  bool get canGoBack => _history.isNotEmpty;

  void back() {
    if (_history.isEmpty) return;
    _destination = _history.removeLast();
    notifyListeners();
  }

  /// Opens the object where it lives and selects it for the inspector.
  void open(ObjectRef ref) {
    final target = switch (ref.kind) {
      ObjectKind.material => Destination.research,
      ObjectKind.monitor => Destination.monitoring,
      ObjectKind.subject => Destination.subjects,
      ObjectKind.client => Destination.clients,
      ObjectKind.release => Destination.community,
      ObjectKind.artifact => Destination.creation,
      ObjectKind.work => Destination.clients,
      ObjectKind.note => _destination,
    };
    go(target);
    select(ref);
  }

  void select(ObjectRef? ref) {
    _selection[_destination] = ref;
    notifyListeners();
  }

  void showTool(ToolTab tab) {
    toolTab = tab;
    if (toolDockable) {
      toolOpen = true;
    } else {
      toolDrawer = true;
    }
    notifyListeners();
  }

  void setOverlay(ShellLayer value) {
    overlay = value;
    notifyListeners();
  }

  /// Set by the shell for the current window width. When a pane cannot dock,
  /// it is recalled as a transient layer that starts closed, so a narrow or
  /// medium window opens on the content rather than on a covering panel.
  bool navDockable = true;
  bool toolDockable = true;
  bool navDrawer = false;
  bool toolDrawer = false;

  bool get navShown => navDockable ? navOpen : navDrawer;
  bool get toolShown => toolDockable ? toolOpen : toolDrawer;

  void toggleNav() {
    if (navDockable) {
      navOpen = !navOpen;
      _save();
    } else {
      navDrawer = !navDrawer;
    }
    notifyListeners();
  }

  void toggleTool() {
    if (toolDockable) {
      toolOpen = !toolOpen;
      _save();
    } else {
      toolDrawer = !toolDrawer;
    }
    notifyListeners();
  }

  void toggleFocus() {
    focusMode = !focusMode;
    notifyListeners();
  }

  void setNavWidth(double w) {
    navWidth = w.clamp(Dock.navMin, Dock.navMax);
    notifyListeners();
  }

  void setToolWidth(double w) {
    toolWidth = w.clamp(Dock.toolMin, Dock.toolMax);
    notifyListeners();
  }

  void commitDock() => _save();

  void setAppearance(Appearance a) {
    appearance = a;
    _save();
    notifyListeners();
  }

  List<ChatMessage> conversation(String key) => [...content.conversation(key), ...?_localMessages[key]];

  /// Keeps the message locally. No AI service is connected in this build, so
  /// the reply says so instead of inventing an answer.
  void sendMessage(String key, String text, {List<ObjectRef> refs = const []}) {
    final list = _localMessages.putIfAbsent(key, () => []);
    list.add(ChatMessage(ChatRole.user, text, refs: refs));
    list.add(const ChatMessage(ChatRole.system, '已记下这条委托。当前是界面示例版，没有连接 AI 与后台服务，不会开始处理；接通后会从这条消息继续。'));
    drafts.remove(key);
    notifyListeners();
  }

  void addCitation(String text) {
    if (!researchCitations.contains(text)) researchCitations.add(text);
    notifyListeners();
  }

  void removeCitation(String text) {
    researchCitations.remove(text);
    notifyListeners();
  }

  SceneController scene(String key, List<SpatialInstance> Function() initial) =>
      _scenes.putIfAbsent(key, () {
        final saved = _prefs?.getString('scene.$key');
        SceneState state;
        try {
          state = saved == null ? SceneState.of(initial()) : SceneState.fromJson((jsonDecode(saved) as Map).cast<String, Object?>());
        } on Object {
          state = SceneState.of(initial());
        }
        return SceneController(state, (s) => _prefs?.setString('scene.$key', jsonEncode(s.toJson())));
      });

  void _restore() {
    final p = _prefs;
    if (p == null) return;
    appearance = Appearance.values.asNameMap()[p.getString('appearance')] ?? Appearance.system;
    navWidth = (p.getDouble('dock.nav') ?? Dock.navDefault).clamp(Dock.navMin, Dock.navMax);
    toolWidth = (p.getDouble('dock.tool') ?? Dock.toolDefault).clamp(Dock.toolMin, Dock.toolMax);
    navOpen = p.getBool('dock.navOpen') ?? true;
    toolOpen = p.getBool('dock.toolOpen') ?? true;
  }

  void _save() {
    final p = _prefs;
    if (p == null) return;
    p.setString('appearance', appearance.name);
    p.setDouble('dock.nav', navWidth);
    p.setDouble('dock.tool', toolWidth);
    p.setBool('dock.navOpen', navOpen);
    p.setBool('dock.toolOpen', toolOpen);
  }
}

/// Owns one desk's scene. Persists only committed revisions, never drag frames.
class SceneController extends ValueNotifier<SceneState> {
  SceneController(super.value, this._persist);

  final void Function(SceneState) _persist;

  void dispatch(SceneCommand command) {
    final before = value.revision;
    value = reduceScene(value, command);
    if (value.revision != before) _persist(value);
  }
}
