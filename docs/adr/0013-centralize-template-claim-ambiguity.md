---
status: accepted
---

# Centralize template claim ambiguity

NetBox keeps no link from an interface to the template that created it, so the plugin decides by a
name claim which template an interface stands for. One claim pass decides it for a module on every
path: an install, a forced reapply after a virtual-chassis or type change, Apply Rules and its
preview, conversion, and the reapply after a move or a bay edit (ADR 0015). The family package owns
the pass (`RawBases`) and its primitive (`TemplateClaim`, `resolve_template_claims`). No stage
accepts or discards a claim before the pass, and every path reads the same result.

The pass collects every form of every template as evidence, in this one order:

1. the template's raw name now;
2. a raw name at an earlier virtual-chassis position that is no template's raw name now, so a raw
   name beats another template's earlier virtual-chassis form;
3. a name the rule gives the template, with `{base}` as the raw name now or at any virtual-chassis
   position and `{vc_position}` at any position; a parent name without `{base}` counts only through
   a channel the rule gave that template's base;
4. a flat family: the plain interfaces that the rule's channel names spell from one of those bases,
   when the module carries the first one; a flat breakout rule gives it, and a channelized rule gave
   it while it was flat;
5. after a move or a bay edit, the raw name and the names the previous rule gave, from the previous
   state. A move recognises no flat family (ADR 0015), so its pass has no flat family forms.

A module type without interface templates claims as one template whose raw name is the bay position.

The primitive takes the complete relation. A claim is a set of units: one interface name, or the
names of one flat family. A template claims one unit when exactly one of its units holds every name
it claims. A template that claims more, or a name that more than one template claims, disqualifies
every claim involved. Repeated units count once, duplicate claimant IDs are invalid, and accepted
pairs keep claimant order. The primitive renders the collision messages and does not log, so the
decision has no side effects; the pass logs each message once through its own logger. The primitive
uses only the standard library and does not discover candidates.

The paths read the result as follows:

- A rule that reads `{base}` gets the raw name of the accepted claim. A name that no single template
  claims has no base, and nothing renames it. After a move every rule reads the claim this way.
  Otherwise a rule that does not read `{base}` takes each name as its own base, and so does a module
  type without templates.
- The engine chooses the scope of an automatic run, and the family package applies it with the
  claim. An install touches an interface that a
  template claims as its raw name, now or at an earlier position. A forced reapply touches every
  interface. An interface in scope that the claim refuses keeps its name and is reported as
  unclaimed. A breakout rule builds no family on an interface that no template claims, so that
  interface keeps its name and is reported as unclaimed too. A subinterface that no template claims
  is no candidate of its own, so a breakout rule neither touches nor reports it. This scope check
  runs before two admitted interfaces that intend one family collapse into it, as required by ADR 0011.
- A flat breakout rule renames a complete flat family that one template alone claims. It keeps and
  reports a family that lost a member, unless the rule gives the family those names already: the
  family's first interface then builds it again. A channelized rule renames no flat family, and
  conversion converts only a flat family that one template alone claims.

Installed-family discovery and the engine's admission guard used to resolve the historical
`{vc_position}` claims in stages of their own, before the raw-name guard admitted the remaining
interfaces. After a renumber, the stages could each accept one of two candidates of one template,
such as a family named at the old position and an interface at the new raw name, where one pass
refuses both and reports them. Keeping the stages and aligning their filters was rejected, because
each stage decides without the evidence the others hold. Dropping a historical base that some
template resolves to now was also rejected: that filter ran before the claim, and a name the rule
gives two templates belongs to neither.
