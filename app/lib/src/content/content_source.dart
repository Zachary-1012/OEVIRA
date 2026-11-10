import 'models.dart';

/// The only way surfaces get content. The sample source implements it today;
/// a backend adapter replaces it without touching widgets.
abstract interface class ContentSource {
  /// True when everything shown is illustrative rather than the user's data.
  bool get isSample;

  String get workspaceName;
  List<Work> get works;
  List<SourceMaterial> get materials;
  List<Artifact> get artifacts;
  List<Monitor> get monitors;
  List<Subject> get subjects;
  List<Client> get clients;
  List<Release> get releases;
  List<BrowserPage> get browserPages;
  MediaProject? mediaProject(String artifactId);
  List<ChatMessage> conversation(String destinationKey);
}

extension ContentLookup on ContentSource {
  Work? work(String id) => _find(works, (w) => w.id == id);
  SourceMaterial? material(String id) => _find(materials, (m) => m.id == id);
  Artifact? artifact(String id) => _find(artifacts, (a) => a.id == id);
  Subject? subject(String id) => _find(subjects, (s) => s.id == id);
  Client? client(String id) => _find(clients, (c) => c.id == id);
  Monitor? monitor(String id) => _find(monitors, (m) => m.id == id);
  Release? release(String id) => _find(releases, (r) => r.id == id);

  String titleOf(ObjectRef ref) => switch (ref.kind) {
        ObjectKind.work => work(ref.id)?.title,
        ObjectKind.material => material(ref.id)?.title,
        ObjectKind.artifact => artifact(ref.id)?.title,
        ObjectKind.monitor => monitor(ref.id)?.title,
        ObjectKind.subject => subject(ref.id)?.name,
        ObjectKind.client => client(ref.id)?.name,
        ObjectKind.release => release(ref.id)?.title,
        ObjectKind.note => '便笺',
      } ??
      '不可用的对象';

  static T? _find<T>(List<T> items, bool Function(T) test) {
    for (final item in items) {
      if (test(item)) return item;
    }
    return null;
  }
}
