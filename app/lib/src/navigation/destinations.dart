import '../brand/line_icons.dart';

enum NavGroup { work, professional, content }

extension NavGroupLabel on NavGroup {
  String get label => switch (this) {
        NavGroup.work => '工作',
        NavGroup.professional => '专业',
        NavGroup.content => '内容',
      };
}

/// Default destinations from PRODUCT-SPEC §5. Browser is a full surface too,
/// reached from the global controls rather than a navigation group.
enum Destination {
  workbench('工作台', NavGroup.work, OIcon.workbench, '继续工作、给目标或打开原件'),
  clients('客户与工作', NavGroup.work, OIcon.clients, '委托、要求、方案与成套交付'),
  subjects('主体与经营', NavGroup.work, OIcon.subjects, '品牌、场所、门店与经营问题'),
  monitoring('监测', NavGroup.professional, OIcon.monitor, '持续关注、事件与检查状态'),
  research('研究', NavGroup.professional, OIcon.research, '原文、反证、引用与判断'),
  creation('创作', NavGroup.professional, OIcon.create, '写作、文件、视频与播客'),
  library('资料与成果', NavGroup.content, OIcon.library, '跨工作找回资料、作品与版本'),
  community('创意社区', NavGroup.content, OIcon.community, '作品、模板与经验'),
  browser('浏览器', null, OIcon.browser, '寰意内浏览与取材');

  const Destination(this.label, this.group, this.icon, this.purpose);

  final String label;
  final NavGroup? group;
  final OIcon icon;
  final String purpose;

  static List<Destination> inGroup(NavGroup group) => values.where((d) => d.group == group).toList();
}
