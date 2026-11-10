/// Typed content the visual client renders. A backend adapter later fills the
/// same types; widgets never read raw maps.
library;

enum ObjectKind { work, material, artifact, monitor, subject, client, release, note }

/// Stable identity of one business object; spatial instances only point here.
class ObjectRef {
  const ObjectRef(this.kind, this.id);
  final ObjectKind kind;
  final String id;

  String get key => '${kind.name}:$id';

  static ObjectRef parse(String key) {
    final split = key.indexOf(':');
    return ObjectRef(ObjectKind.values.byName(key.substring(0, split)), key.substring(split + 1));
  }

  @override
  bool operator ==(Object other) => other is ObjectRef && other.kind == kind && other.id == id;
  @override
  int get hashCode => Object.hash(kind, id);
}

enum WorkState { inProgress, waitingInput, review, delivered, paused }

extension WorkStateLabel on WorkState {
  String get label => switch (this) {
        WorkState.inProgress => '进行中',
        WorkState.waitingInput => '待补充',
        WorkState.review => '客户审阅',
        WorkState.delivered => '已交付',
        WorkState.paused => '已暂停',
      };
}

class Work {
  const Work({
    required this.id,
    required this.title,
    required this.goal,
    required this.state,
    required this.updated,
    required this.nextStep,
    this.clientId,
    this.subjectIds = const [],
    this.materialIds = const [],
    this.artifactIds = const [],
    this.conditions = const [],
    this.timeline = const [],
  });

  final String id;
  final String title;
  final String goal;
  final WorkState state;
  final String updated;
  final String nextStep;
  final String? clientId;
  final List<String> subjectIds;
  final List<String> materialIds;
  final List<String> artifactIds;
  final List<Condition> conditions;
  final List<TimelineEntry> timeline;
}

class Condition {
  const Condition(this.label, this.value, {this.confirmed = true});
  final String label;
  final String value;
  final bool confirmed;
}

class TimelineEntry {
  const TimelineEntry(this.time, this.text, {this.actor = '我'});
  final String time;
  final String text;
  final String actor;
}

enum MaterialKind { report, interview, webpage, table, note, transcript }

extension MaterialKindLabel on MaterialKind {
  String get label => switch (this) {
        MaterialKind.report => '报告',
        MaterialKind.interview => '访谈',
        MaterialKind.webpage => '网页',
        MaterialKind.table => '数据表',
        MaterialKind.note => '笔记',
        MaterialKind.transcript => '转写稿',
      };
}

class Segment {
  const Segment(this.label, this.text, {this.heading = false});
  final String label;
  final String text;
  final bool heading;
}

/// The back face of a material: version, source, method and licence.
class Provenance {
  const Provenance({
    required this.version,
    required this.source,
    required this.capturedAt,
    required this.method,
    required this.license,
    required this.fingerprint,
    this.limitations,
  });

  final String version;
  final String source;
  final String capturedAt;
  final String method;
  final String license;
  final String fingerprint;
  final String? limitations;
}

/// An original material (named apart from Flutter's Material widget).
class SourceMaterial {
  const SourceMaterial({
    required this.id,
    required this.title,
    required this.kind,
    required this.pages,
    required this.provenance,
    this.workId,
    this.tags = const [],
    this.summary = '',
  });

  final String id;
  final String title;
  final MaterialKind kind;
  final List<Segment> pages;
  final Provenance provenance;
  final String? workId;
  final List<String> tags;
  final String summary;
}

enum ArtifactKind { article, word, ppt, sheet, pdf, html, video, podcast }

extension ArtifactKindLabel on ArtifactKind {
  String get label => switch (this) {
        ArtifactKind.article => '文稿',
        ArtifactKind.word => 'Word',
        ArtifactKind.ppt => 'PPT',
        ArtifactKind.sheet => '工作簿',
        ArtifactKind.pdf => 'PDF',
        ArtifactKind.html => '动态 HTML',
        ArtifactKind.video => '视频工程',
        ArtifactKind.podcast => '播客工程',
      };
}

class Artifact {
  const Artifact({
    required this.id,
    required this.title,
    required this.kind,
    required this.version,
    required this.updated,
    required this.status,
    this.workId,
    this.outline = const [],
    this.comments = const [],
    this.sections = const [],
    this.data = const {},
  });

  final String id;
  final String title;
  final ArtifactKind kind;
  final int version;
  final String updated;
  final String status;
  final String? workId;
  final List<String> outline;
  final List<ReviewComment> comments;

  /// Body text per outline entry, same order. Empty means the body is not
  /// part of this sample and the preview shows the outline only.
  final List<String> sections;

  /// Named series for dynamic HTML artifacts; gaps stay null.
  final Map<String, List<double?>> data;
}

class ReviewComment {
  const ReviewComment(this.author, this.anchor, this.text, {this.resolved = false});
  final String author;
  final String anchor;
  final String text;
  final bool resolved;
}

enum MonitorState { watching, checking, paused, failed }

extension MonitorStateLabel on MonitorState {
  String get label => switch (this) {
        MonitorState.watching => '监测中',
        MonitorState.checking => '正在检查',
        MonitorState.paused => '已暂停',
        MonitorState.failed => '来源失败',
      };
}

enum Stance { positive, neutral, negative, question }

extension StanceLabel on Stance {
  String get label => switch (this) {
        Stance.positive => '正面',
        Stance.neutral => '中性',
        Stance.negative => '负面',
        Stance.question => '询问',
      };
}

class MonitorEvent {
  const MonitorEvent({
    required this.time,
    required this.source,
    required this.title,
    required this.excerpt,
    required this.stance,
    this.handled = false,
    this.replies = 0,
  });

  final String time;
  final String source;
  final String title;
  final String excerpt;
  final Stance stance;
  final bool handled;
  final int replies;
}

class SourceCheck {
  const SourceCheck(this.source, this.lastChecked, this.result);
  final String source;
  final String lastChecked;
  final String result;
}

class Monitor {
  const Monitor({
    required this.id,
    required this.title,
    required this.scope,
    required this.state,
    required this.keywords,
    required this.checks,
    required this.events,
    this.workId,
  });

  final String id;
  final String title;
  final String scope;
  final MonitorState state;
  final List<String> keywords;
  final List<SourceCheck> checks;
  final List<MonitorEvent> events;
  final String? workId;
}

enum SubjectKind { brand, organization, venue, store }

extension SubjectKindLabel on SubjectKind {
  String get label => switch (this) {
        SubjectKind.brand => '品牌',
        SubjectKind.organization => '法律组织',
        SubjectKind.venue => '商业场所',
        SubjectKind.store => '门店',
      };
}

class Relation {
  const Relation(this.role, this.targetId, this.period, this.evidence);
  final String role;
  final String targetId;
  final String period;
  final String evidence;
}

class Fact {
  const Fact(this.label, this.value, this.source, {this.pending = false});
  final String label;
  final String value;
  final String source;
  final bool pending;
}

/// One operating question laid out as question → change → reason → evidence → action.
class Insight {
  const Insight({
    required this.question,
    required this.change,
    required this.series,
    required this.seriesLabel,
    required this.reasons,
    required this.evidence,
    required this.actions,
  });

  final String question;
  final String change;
  final List<double?> series;
  final String seriesLabel;
  final List<String> reasons;
  final List<String> evidence;
  final List<String> actions;
}

class Subject {
  const Subject({
    required this.id,
    required this.name,
    required this.kind,
    required this.city,
    this.facts = const [],
    this.relations = const [],
    this.insight,
  });

  final String id;
  final String name;
  final SubjectKind kind;
  final String city;
  final List<Fact> facts;
  final List<Relation> relations;
  final Insight? insight;
}

class Commission {
  const Commission(this.title, this.scope, this.due, this.state, {this.workIds = const []});
  final String title;
  final String scope;
  final String due;
  final String state;
  final List<String> workIds;
}

class Client {
  const Client({required this.id, required this.name, required this.contact, required this.commissions, this.boundary = ''});
  final String id;
  final String name;
  final String contact;
  final List<Commission> commissions;
  final String boundary;
}

enum ReleaseKind { article, gallery, video, podcast, template, experience }

extension ReleaseKindLabel on ReleaseKind {
  String get label => switch (this) {
        ReleaseKind.article => '文章',
        ReleaseKind.gallery => '多图',
        ReleaseKind.video => '视频',
        ReleaseKind.podcast => '播客',
        ReleaseKind.template => '模板',
        ReleaseKind.experience => '经验',
      };
}

class Release {
  const Release({
    required this.id,
    required this.title,
    required this.author,
    required this.kind,
    required this.published,
    required this.summary,
    this.duration,
    this.images = 0,
    this.aspect = 16 / 9,
    this.license = '仅寰意内观看',
    this.body = const [],
  });

  final String id;
  final String title;
  final String author;
  final ReleaseKind kind;
  final String published;
  final String summary;
  final String? duration;
  final int images;
  final double aspect;
  final String license;
  final List<String> body;
}

enum ChatRole { user, assistant, system }

class ChatMessage {
  const ChatMessage(this.role, this.text, {this.refs = const []});
  final ChatRole role;
  final String text;
  final List<ObjectRef> refs;
}

class MediaTrack {
  const MediaTrack(this.name, this.kind, this.clips);
  final String name;
  final String kind;
  final List<MediaClip> clips;
}

class MediaClip {
  const MediaClip(this.label, this.start, this.end);
  final String label;
  final double start;
  final double end;
}

class MediaProject {
  const MediaProject({required this.artifactId, required this.duration, required this.fps, required this.tracks, required this.markers});
  final String artifactId;
  final double duration;
  final int fps;
  final List<MediaTrack> tracks;
  final List<String> markers;
}

class BrowserPage {
  const BrowserPage({required this.title, required this.address, required this.sections});
  final String title;
  final String address;
  final List<Segment> sections;
}
