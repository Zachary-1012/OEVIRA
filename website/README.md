# OEVIRA public website

Astro owns public product/help content, genuine demonstrations and real product entrypoints. It owns no customer data, private drafts, identities or business execution. The route/content contract is in [PRODUCT-SPEC](../docs/PRODUCT-SPEC.md#官网的路由内容和真实下一步); implementation follows [FRONTEND-EXECUTION](../docs/FRONTEND-EXECUTION.md#官网实施固定路由与可验证公开内容).

## Dependency preparation and implementation are different

The selected framework is Astro 7.3.3 with Node.js >=22.12.0 and npm >=9.6.5. The authoritative implementation checkout uses `website/package.json` and `website/package-lock.json`; do not recover the old website's appearance, source pages or production configuration to obtain a passing build. Dependency preparation does not make the website implemented, publicly available or accepted.

The existing local preparation pins advisory-remediation overrides for sharp 0.35.5, smol-toml 1.9.0, source-map-js 1.2.2, undici 8.12.2, vite 8.3.3 and zod 4.6.6. The lock fixes their transitive/native artifacts; preserve its integrity values and the actual package notices. These are targeted compatibility/security pins, not a policy to track every latest release. [Third-party notices](../THIRD_PARTY_NOTICES.md#public-website-build-dependencies) describe the boundaries.

In a checkout that actually contains both manifests, preparation uses `npm ci --ignore-scripts --no-fund` from `website`, followed by `npm ls --all` and the declared compiler/native-image checks. Read and review any required lifecycle scripts before enabling them; do not globally enable scripts to fix a single package. A specification-only checkout may intentionally contain only this responsibility document: missing manifests are reported precisely, not recreated from guessed versions or replaced with `npm install latest`. Local unpublished dependency files are accounted for with the other development work until an approved implementation publication includes them.

## Determined website behavior

Use one static Astro site, `zh-CN/en/ko`, locale-prefixed content and a root language selector. Set `prefixDefaultLocale: true` and `redirectToDefaultLocale: false` explicitly. Product/solutions/capabilities/workflows/works/cases/help/trust/policies/releases/download/about/contact/status and real 404 follow the specification, not placeholder page counts. Only genuinely public content can enter a static build; current publication/media access and withdrawal remain with the product's existing owner. No new CMS, analytics, hosting or search provider is implied.

Isolated compiler fixtures belong in ignored test storage, not `website/src`; a three-language fixture does not establish actual website copy, visual quality, accessibility, HTTP hosting or deployment. Production work must verify real content and permitted assets, genuine release/download/contact targets, per-language navigation, error recovery and the exact published build. The specification baseline does not claim a website implementation or deployment.
