"""Deterministic, explainable identity resolution (no ML, no LLM).

    source identities → signatures (dedupe within source)
      → shared-value detection (phones/addresses/domains asserted by many
        different names are non-identifying)
      → blocking (license, phone, address, domain, production reference;
        oversized blocks are capped, never compared all-pairs)
      → evidence evaluation → union with guards (license conflicts and
        name-chaining fail closed)
      → name-only grouping / attachment (CANDIDATE only, never confident)
      → canonical companies, confidence states, decisions, reviews
      → activity links (activity never decides existence)

Rules (all require name agreement — a shared phone/address/domain alone never
links two differently named businesses):
  LICENSE_NAME            identity's ROC license = ROC holder, names exact or related
  LICENSE_NAME_PEER       two non-ROC identities with the same license, names exact
  ROC_SAME_NAME_ADDRESS   ROC licenses (classes) with the same holder name and address
  PHONE_NAME              same non-shared phone, exact name
  ADDRESS_NAME            same non-shared street (+ZIP or city), exact name
  DOMAIN_NAME             same non-shared domain/email, exact name
  PROD_REFERENCE          contact channel researched for a production company, joined
                          to that company's permit identity of the same name
  NAME_ONLY_GROUP         unlocated business identities with an identical name (CANDIDATE)
  NAME_ATTACH             a name group attached to the single located company of that name
  CROSSWALK_ATTACH        a name group attached via CorridorIQ's prior ROC crosswalk (CANDIDATE)
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from datetime import date, timedelta

from pipeline.identity_shadow import normalize as N
from pipeline.identity_shadow.config import RULES_VERSION, ShadowConfig, family_class
from pipeline.identity_shadow.schema import DERIVED_TABLES

log = logging.getLogger("corridoriq.identity_shadow")

STRONG_RULES = {"LICENSE_NAME", "LICENSE_NAME_PEER", "ROC_SAME_NAME_ADDRESS", "PHONE_NAME", "ADDRESS_NAME",
                "DOMAIN_NAME", "PROD_REFERENCE"}
CANDIDATE_RULES = {"NAME_ONLY_GROUP", "NAME_ATTACH", "CROSSWALK_ATTACH"}
STATE_ORDER = ["CONFLICT", "REVIEW_REQUIRED", "VERIFIED", "HIGH_CONFIDENCE", "CANDIDATE", "SOURCE_ONLY"]


def _id(*parts) -> str:
    return hashlib.sha1("\x1f".join("" if p is None else str(p) for p in parts).encode("utf-8")).hexdigest()[:24]


class Sig:
    __slots__ = ("sid", "family", "kind", "name", "dba", "phone", "street", "zip5", "city", "email", "domain",
                 "license", "historical", "strong_only", "identity_ids", "prod_ids", "roles", "originals")

    def __init__(self, sid, family, kind, name, dba, phone, street, zip5, city, email, domain, license_,
                 historical, strong_only):
        self.sid, self.family, self.kind = sid, family, kind
        self.name, self.dba, self.phone, self.street, self.zip5, self.city = name, dba, phone, street, zip5, city
        self.email, self.domain, self.license = email, domain, license_
        self.historical, self.strong_only = historical, strong_only
        self.identity_ids, self.prod_ids, self.roles, self.originals = [], set(), Counter(), Counter()

    @property
    def names(self):
        return {k for k in (self.name, self.dba) if k}

    @property
    def located(self):
        # A business location needs a ZIP or city. A street alone (e.g. Mesa's
        # contractor_address) can corroborate a same-name match but does not
        # make an identity "located" for ambiguity decisions.
        return bool(not self.historical and (self.zip5 or self.city))


class _UF:
    def __init__(self, n):
        self.p = list(range(n))
        self.names = [set() for _ in range(n)]
        self.lic = [set() for _ in range(n)]
        self.streets = [set() for _ in range(n)]

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def merge(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        if len(self.names[ra]) < len(self.names[rb]):
            ra, rb = rb, ra
        self.p[rb] = ra
        self.names[ra] |= self.names[rb]
        self.lic[ra] |= self.lic[rb]
        self.streets[ra] |= self.streets[rb]
        return ra


class Resolver:
    def __init__(self, shadow, cfg: ShadowConfig, batch_id: str):
        self.s, self.cfg, self.batch = shadow, cfg, batch_id
        self.sigs: list[Sig] = []
        self.decisions: dict[str, tuple] = {}
        self.reviews: dict[str, tuple] = {}
        self.edges: list[tuple[int, int, str]] = []
        self.join_rule: dict[int, str] = {}
        self.conflict_sigs: set[int] = set()
        self.review_sigs: set[int] = set()
        self.metrics = Counter()
        self.held: list[tuple] = []          # (identity_id, reason) never canonicalized
        self.crosswalk_lic: dict[int, set] = defaultdict(set)   # prod company → licenses (hint only)

    # ------------------------------------------------------------- load
    def load(self):
        by_sig: dict[tuple, Sig] = {}
        rows = self.s.execute(
            "SELECT identity_id, family, role, entity_kind, eligible, state, original_name, name_key, dba_key, "
            "phone_key, street_key, zip5, city_key, email_key, domain_key, license_key, prod_company_id "
            "FROM source_identities WHERE last_batch_id=?", (self.batch,))
        for r in rows:
            if r["family"] == "roc_crosswalk":
                if r["prod_company_id"] and r["license_key"]:
                    self.crosswalk_lic[r["prod_company_id"]].add(r["license_key"])
                self.metrics["crosswalk_hints"] += 1
                continue
            historical = r["state"] == "SUPERSEDED"
            if not r["eligible"]:
                has_strong = bool(r["phone_key"] or r["street_key"]) and r["entity_kind"] == "PERSON" \
                    and r["role"] not in ("applicant_contact",)
                if not has_strong:
                    reason = "contact_person" if r["role"] == "applicant_contact" else "person_name_uncorroborated"
                    self.held.append((r["identity_id"], reason, r["name_key"], r["family"], r["prod_company_id"]))
                    continue
            strong_only = r["entity_kind"] == "PERSON" and not r["license_key"]
            key = (r["family"], r["entity_kind"], r["name_key"], r["dba_key"], r["phone_key"], r["street_key"],
                   r["zip5"], r["city_key"], r["email_key"], r["domain_key"], r["license_key"], historical,
                   strong_only)
            sig = by_sig.get(key)
            if sig is None:
                sig = Sig(_id("sig", *key), r["family"], r["entity_kind"], r["name_key"], r["dba_key"],
                          r["phone_key"], r["street_key"], r["zip5"], r["city_key"], r["email_key"],
                          r["domain_key"], r["license_key"], historical, strong_only)
                by_sig[key] = sig
            sig.identity_ids.append(r["identity_id"])
            if r["prod_company_id"]:
                sig.prod_ids.add(r["prod_company_id"])
            sig.roles[r["role"]] += 1
            sig.originals[r["original_name"]] += 1
        self.sigs = sorted(by_sig.values(), key=lambda s: s.sid)
        self.uf = _UF(len(self.sigs))
        for i, s in enumerate(self.sigs):
            self.uf.names[i] = set(s.names)
            if s.license:
                self.uf.lic[i] = {s.license}
            if s.street and not s.historical:
                self.uf.streets[i] = {s.street}
        self.metrics["signatures"] = len(self.sigs)
        self.metrics["held_identities"] = len(self.held)

    # ------------------------------------------------------- shared values
    def _shared(self, attr):
        names = defaultdict(set)
        for s in self.sigs:
            v = getattr(s, attr)
            if v and not s.historical and s.name:
                names[v].add(s.name)
        return {v for v, ns in names.items() if len(ns) > self.cfg.shared_value_names}

    # ------------------------------------------------------------- unions
    def _union(self, a, b, rule, evidence):
        ra, rb = self.uf.find(a), self.uf.find(b)
        if ra == rb:
            return True
        names = self.uf.names[ra] | self.uf.names[rb]
        if len(names) > self.cfg.max_component_names:
            self._review("CHAIN_CAP", (a, b), "joining would chain too many different names",
                         evidence, [], "Review whether these identities are one business")
            self.review_sigs.update((a, b))
            self.metrics["refused_chain_cap"] += 1
            return False
        la, lb = self.uf.lic[ra], self.uf.lic[rb]
        if la and lb and not (la & lb):
            shared_street = self.uf.streets[ra] & self.uf.streets[rb]
            if not shared_street:
                self._review("LICENSE_CONFLICT", (a, b),
                             "two different ROC licenses would join without a shared business address",
                             evidence, ["different licenses"], "Confirm whether one business holds both licenses")
                self.review_sigs.update((a, b))
                self.metrics["refused_license_conflict"] += 1
                return False
        self.uf.merge(ra, rb)
        self.edges.append((a, b, rule))
        for x in (a, b):
            prev = self.join_rule.get(x)
            if prev is None or (prev in CANDIDATE_RULES and rule in STRONG_RULES):
                self.join_rule[x] = rule
        self.metrics[f"links:{rule}"] += 1
        return True

    def _review(self, kind, subject_idx, reason, supporting, conflicting, action):
        subject = "|".join(self.sigs[i].sid for i in subject_idx)
        rid = _id(RULES_VERSION, kind, subject)
        fams = sorted({self.sigs[i].family for i in subject_idx})
        self.reviews[rid] = (rid, kind, subject, json.dumps([self.sigs[i].sid for i in subject_idx]),
                             json.dumps(sorted(supporting)), json.dumps(sorted(conflicting)), ",".join(fams),
                             reason, action)

    def _block(self, index, attr_check, rule, shared):
        """index: key -> [sig idx]. Compare within capped blocks; link on exact name."""
        cap = self.cfg.block_cap
        for key, members in index.items():
            if key in shared:
                self.metrics[f"blocks_shared_skipped:{rule}"] += 1
                continue
            if len(members) < 2:
                continue
            self.metrics[f"block_max:{rule}"] = max(self.metrics[f"block_max:{rule}"], len(members))
            if len(members) > cap:
                self.metrics[f"blocks_capped:{rule}"] += 1
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    sa, sb = self.sigs[a], self.sigs[b]
                    if not attr_check(sa, sb):
                        continue
                    if N.names_agree(sa.names, sb.names) == "exact":
                        self._union(a, b, rule, [rule.split("_")[0].lower(), "name_exact"])
                    else:
                        self.metrics[f"collisions:{rule}"] += 1

    def strong_unions(self):
        sigs = self.sigs
        # 1. License.
        roc_by_lic, others_by_lic = {}, defaultdict(list)
        for i, s in enumerate(sigs):
            if not s.license:
                continue
            if s.family == "roc":
                roc_by_lic[s.license] = i
            else:
                others_by_lic[s.license].append(i)
        for lic, members in others_by_lic.items():
            r = roc_by_lic.get(lic)
            for i in members:
                if r is not None:
                    agree = N.names_agree(sigs[i].names, sigs[r].names)
                    if agree:
                        self._union(i, r, "LICENSE_NAME", ["license_match", f"name_{agree}"])
                    else:
                        self.conflict_sigs.add(i)
                        self._review("LICENSE_NAME_CONFLICT", (i, r),
                                     "identity cites a ROC license held by a differently named business",
                                     ["license_match"], ["name_disagrees"],
                                     "Check whether the permit names the license holder or another party")
                        self.metrics["license_name_conflicts"] += 1
            if r is None and len(members) > 1:
                for x in range(len(members)):
                    for y in range(x + 1, len(members)):
                        a, b = members[x], members[y]
                        if N.names_agree(sigs[a].names, sigs[b].names) == "exact":
                            self._union(a, b, "LICENSE_NAME_PEER", ["license_match", "name_exact"])
        # 2. ROC: one business, several licenses (classes) at one address.
        groups = defaultdict(list)
        for i, s in enumerate(sigs):
            if s.family == "roc" and s.street:
                for nk in s.names:
                    groups[(nk, s.street, s.zip5 or s.city or "")].append(i)
        for members in groups.values():
            for i in members[1:]:
                self._union(members[0], i, "ROC_SAME_NAME_ADDRESS", ["name_exact", "address_match", "multi_license"])
        # 3-5. Phone, address, domain/email — names must agree exactly.
        shared_phone, shared_street, shared_domain = self._shared("phone"), self._shared("street"), self._shared("domain")
        self.shared = {"phone": shared_phone, "street": shared_street, "domain": shared_domain}
        self.metrics["shared_phones"] = len(shared_phone)
        self.metrics["shared_streets"] = len(shared_street)
        self.metrics["shared_domains"] = len(shared_domain)
        by_phone, by_street, by_domain, by_email = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
        for i, s in enumerate(sigs):
            if s.historical:
                continue
            if s.phone:
                by_phone[s.phone].append(i)
            if s.street:
                by_street[s.street].append(i)
            if s.domain:
                by_domain[s.domain].append(i)
            if s.email:
                by_email[s.email].append(i)
        self._block(by_phone, lambda a, b: True, "PHONE_NAME", shared_phone)

        def same_place(a, b):
            if a.zip5 and b.zip5:
                return a.zip5 == b.zip5
            if a.city and b.city:
                return a.city == b.city
            return True  # one side has street only (e.g. Mesa contractor_address); name must still agree
        self._block(by_street, same_place, "ADDRESS_NAME", shared_street)
        self._block(by_domain, lambda a, b: True, "DOMAIN_NAME", shared_domain)
        self._block(by_email, lambda a, b: True, "DOMAIN_NAME", set())
        # 6. Production reference: contact channels → that company's permit identity of the same name.
        by_prod = defaultdict(list)
        for i, s in enumerate(sigs):
            for pid in s.prod_ids:
                by_prod[pid].append(i)
        for pid, members in by_prod.items():
            chans = [i for i in members if sigs[i].family.startswith("contact:")]
            permits = [i for i in members if sigs[i].family.startswith("permit:")]
            for c in chans:
                for p in permits:
                    if N.names_agree(sigs[c].names, sigs[p].names) == "exact":
                        self._union(c, p, "PROD_REFERENCE", ["production_reference", "name_exact"])

    def name_attachment(self):
        sigs, uf = self.sigs, self.uf
        comp_members = defaultdict(list)
        for i in range(len(sigs)):
            comp_members[uf.find(i)].append(i)
        located = {r for r, ms in comp_members.items() if any(sigs[i].located for i in ms)}
        # Unlocated business components: group by identical name (CANDIDATE only).
        first_by_name: dict[str, int] = {}
        for r, ms in sorted(comp_members.items(), key=lambda kv: sigs[kv[1][0]].sid):
            if r in located or any(sigs[i].strong_only or sigs[i].kind == "PERSON" for i in ms):
                continue
            for n in sorted({nm for i in ms for nm in sigs[i].names}):
                if n in first_by_name:
                    self._union(first_by_name[n], ms[0], "NAME_ONLY_GROUP", ["name_exact"])
                else:
                    first_by_name[n] = ms[0]
        # Recompute components; attach unlocated groups to located ones.
        comp_members = defaultdict(list)
        for i in range(len(sigs)):
            comp_members[uf.find(i)].append(i)
        located = {r for r, ms in comp_members.items() if any(sigs[i].located for i in ms)}
        located_by_name = defaultdict(set)
        for r in located:
            for i in comp_members[r]:
                if not sigs[i].strong_only:
                    for n in sigs[i].names:
                        located_by_name[n].add(r)
        for r, ms in list(comp_members.items()):
            if r in located:
                continue
            if any(sigs[i].strong_only or sigs[i].kind == "PERSON" for i in ms):
                continue
            names = {n for i in ms for n in sigs[i].names}
            targets = set()
            for n in names:
                targets |= located_by_name.get(n, set())
            targets = {uf.find(t) for t in targets}
            if not targets:
                continue
            if len(targets) == 1:
                self._union(ms[0], comp_members[next(iter(targets))][0], "NAME_ATTACH", ["name_exact"])
                continue
            # Ambiguous: try the existing crosswalk (hint, not proof).
            hinted = set()
            for i in ms:
                for pid in sigs[i].prod_ids:
                    for lic in self.crosswalk_lic.get(pid, ()):
                        for t in targets:
                            if lic in uf.lic[t]:
                                hinted.add(t)
            if len(hinted) == 1:
                self._union(ms[0], comp_members[next(iter(hinted))][0], "CROSSWALK_ATTACH",
                            ["name_exact", "prior_crosswalk"])
            else:
                self.review_sigs.update(ms)
                self._review("AMBIGUOUS_NAME", tuple(ms[:1]) + tuple(comp_members[t][0] for t in sorted(targets)),
                             f"name matches {len(targets)} separately located businesses",
                             ["name_exact"], ["different_locations"],
                             "Choose the business these observations belong to, or keep separate")
        # Same name, different located businesses: never merged; flagged.
        comp_members = defaultdict(list)
        for i in range(len(sigs)):
            comp_members[uf.find(i)].append(i)
        located = {r for r, ms in comp_members.items() if any(sigs[i].located for i in ms)}
        located_by_name = defaultdict(set)
        for r in located:
            for i in comp_members[r]:
                for n in sigs[i].names:
                    located_by_name[n].add(r)
        for n, roots in located_by_name.items():
            if len(roots) < 2 or len(roots) > self.cfg.block_cap:
                if len(roots) > self.cfg.block_cap:
                    self.metrics["same_name_groups_capped"] += 1
                continue
            cities = {r: {sigs[i].city or sigs[i].zip5 for i in comp_members[r] if sigs[i].located} for r in roots}
            ordered = sorted(roots, key=lambda r: sigs[comp_members[r][0]].sid)
            for x in range(len(ordered)):
                for y in range(x + 1, len(ordered)):
                    a, b = ordered[x], ordered[y]
                    same_area = bool(cities[a] & cities[b])
                    kind = "SAME_NAME_SAME_AREA" if same_area else "SAME_NAME_DIFFERENT_LOCATION"
                    self._review(kind, (comp_members[a][0], comp_members[b][0]),
                                 "same name, separately evidenced business locations"
                                 + ("; same area — possible duplicate" if same_area else ""),
                                 ["name_exact"], [] if same_area else ["different_locations"],
                                 "Merge only with corroborating license/phone/address" if same_area
                                 else "Keep separate unless corroborated")
                    self.metrics[f"pairs:{kind}"] += 1

    def held_person_reviews(self):
        """Person-name identities without corroboration are held (not companies).
        If the name uniquely equals a ROC sole-proprietor licensee, suggest review."""
        roc_person = defaultdict(set)
        for i, s in enumerate(self.sigs):
            if s.family == "roc" and s.kind == "PERSON":
                roc_person[s.name].add(self.uf.find(i))
        for identity_id, reason, name, family, prod in self.held:
            if reason == "person_name_uncorroborated" and name in roc_person and len(roc_person[name]) == 1:
                rid = _id(RULES_VERSION, "PERSON_MATCHES_LICENSEE", identity_id)
                target = next(iter(roc_person[name]))
                self.reviews[rid] = (rid, "PERSON_MATCHES_LICENSEE", identity_id,
                                     json.dumps([self.sigs[target].sid]), json.dumps(["name_exact"]),
                                     json.dumps(["no_business_or_license_corroboration"]), family,
                                     "person-like name equals a sole-proprietor ROC licensee",
                                     "Confirm with license, phone or address before linking")
                self.metrics["person_review_suggestions"] += 1

    # ----------------------------------------------------------- materialize
    def materialize(self, as_of: date):
        sigs, uf, s = self.sigs, self.uf, self.s
        for t in DERIVED_TABLES:
            if t not in ("shadow_diff", "shadow_metrics"):
                s.execute(f"DELETE FROM {t}")
        comps = defaultdict(list)
        for i in range(len(sigs)):
            comps[uf.find(i)].append(i)
        strong_fams = defaultdict(set)
        for a, b, rule in self.edges:
            if rule in STRONG_RULES:
                r = uf.find(a)
                strong_fams[r] |= {sigs[a].family, sigs[b].family}
        canon_of_sig = {}
        canon_rows, sig_rows, dec_rows = [], [], []
        recent_cut = (as_of - timedelta(days=self.cfg.recent_days)).isoformat()
        ident_sig = {}
        for i, sg in enumerate(sigs):
            for iid in sg.identity_ids:
                ident_sig[iid] = i
        for r, ms in comps.items():
            members = sorted(ms, key=lambda i: sigs[i].sid)
            if all(sigs[i].strong_only for i in members):
                # Person names joined only to each other: still not a company.
                for i in members:
                    sig_rows.append((sigs[i].sid, sigs[i].family, sigs[i].kind, len(sigs[i].identity_ids), None, "HELD"))
                    for iid in sigs[i].identity_ids:
                        self.held.append((iid, "person_name_uncorroborated", sigs[i].name, sigs[i].family,
                                          next(iter(sigs[i].prod_ids), None)))
                continue
            cid = "cc_" + _id("canonical", sigs[members[0]].sid)
            for i in members:
                canon_of_sig[i] = cid
            fams = {sigs[i].family for i in members}
            indep = {f for f in fams if family_class(f)[1]}
            corr = {f for f in strong_fams.get(r, set()) if family_class(f)[1]}
            has_license = any(sigs[i].license and sigs[i].family == "roc" for i in members)
            if any(i in self.conflict_sigs for i in members):
                state = "CONFLICT"
            elif any(i in self.review_sigs for i in members):
                state = "REVIEW_REQUIRED"
            elif has_license and "roc" in corr and len(corr) >= 2:
                state = "VERIFIED"
            elif len(corr) >= 2:
                state = "HIGH_CONFIDENCE"
            elif len(indep) >= 2 or len(members) > 1 and len(fams) >= 2:
                state = "CANDIDATE"
            else:
                state = "SOURCE_ONLY"
            roc_members = [i for i in members if sigs[i].family == "roc"]
            if roc_members and all(sigs[i].kind == "PERSON" for i in roc_members):
                kind = "SOLE_PROPRIETOR"
            elif all(sigs[i].kind == "PERSON" for i in members):
                kind = "SOLE_PROPRIETOR"
            elif all(sigs[i].kind == "UNKNOWN" for i in members):
                kind = "UNKNOWN"
            else:
                kind = "BUSINESS"
            name_counts = Counter()
            for i in (roc_members or members):
                name_counts.update(sigs[i].originals)
            display = sorted(name_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if name_counts else ""
            prods = set().union(*(sigs[i].prod_ids for i in members))
            canon_rows.append([cid, display, kind, state, len(fams), len(indep), len(corr), len(members),
                               sum(len(sigs[i].identity_ids) for i in members), 1 if has_license else 0,
                               0, 0, 0, None, len(prods), RULES_VERSION, self.batch])
            for i in members:
                rule = self.join_rule.get(i, "SEED")
                link = "STRONG" if rule in STRONG_RULES else ("CANDIDATE" if rule in CANDIDATE_RULES else "SEED")
                if i in self.conflict_sigs:
                    link = "CONFLICT"
                elif i in self.review_sigs:
                    link = "REVIEW"
                sig_rows.append((sigs[i].sid, sigs[i].family, sigs[i].kind, len(sigs[i].identity_ids), cid, link))
                dec_rows.append((_id(RULES_VERSION, sigs[i].sid), sigs[i].sid, cid,
                                 "CONFLICT" if link == "CONFLICT" else ("REVIEW" if link == "REVIEW" else
                                 ("LINK_STRONG" if link == "STRONG" else ("ATTACH_CANDIDATE" if link == "CANDIDATE"
                                                                           else "SEED"))),
                                 rule, json.dumps(sorted(sigs[i].roles)), json.dumps([]), RULES_VERSION))
        for identity_id, reason, name, family, prod in self.held:
            dec_rows.append((_id(RULES_VERSION, "held", identity_id), identity_id, None, "HOLD", reason,
                             json.dumps([family]), json.dumps([]), RULES_VERSION))
        s.executemany("INSERT INTO identity_signatures VALUES (?,?,?,?,?,?)", sig_rows)
        link_rows = []
        for i, sg in enumerate(sigs):
            cid = canon_of_sig.get(i)
            for iid in sg.identity_ids:
                link_rows.append((iid, sg.sid, cid, "HELD" if cid is None else
                                  ("CONFLICT" if i in self.conflict_sigs else
                                   "REVIEW" if i in self.review_sigs else
                                   "STRONG" if self.join_rule.get(i, "SEED") in STRONG_RULES else
                                   "CANDIDATE" if self.join_rule.get(i) in CANDIDATE_RULES else "SEED"),
                                  "person_name_uncorroborated" if cid is None else None))
        linked = {r[0] for r in link_rows}
        for identity_id, reason, _n, _f, _p in self.held:
            if identity_id not in linked:
                link_rows.append((identity_id, None, None, "HELD", reason))
                linked.add(identity_id)
        s.executemany("INSERT INTO identity_links VALUES (?,?,?,?,?)", link_rows)
        s.executemany("INSERT INTO resolution_decisions VALUES (?,?,?,?,?,?,?,?)", dec_rows)
        s.executemany("INSERT INTO resolution_reviews VALUES (?,?,?,?,?,?,?,?,?)", list(self.reviews.values()))

        # Activity (separate from identity): current permit identities only.
        act = {}
        for r in s.execute("SELECT identity_id, role, prod_permit_id, observed_at FROM source_identities "
                           "WHERE prod_permit_id IS NOT NULL AND state='CURRENT'"):
            i = ident_sig.get(r["identity_id"])
            if i is None or i not in canon_of_sig:
                continue
            cid = canon_of_sig[i]
            rule = self.join_rule.get(i, "SEED")
            link = "STRONG" if rule in STRONG_RULES else ("CANDIDATE" if rule in CANDIDATE_RULES else "SEED")
            act[(cid, r["prod_permit_id"], r["role"])] = (cid, r["prod_permit_id"], r["role"], link, r["observed_at"])
        s.executemany("INSERT INTO company_activity_links VALUES (?,?,?,?,?)", list(act.values()))
        per = defaultdict(lambda: [set(), set(), None])
        for cid, pid, _role, _link, d in act.values():
            per[cid][0].add(pid)
            if d and d >= recent_cut:
                per[cid][1].add(pid)
            if d and (per[cid][2] is None or d > per[cid][2]):
                per[cid][2] = d
        for row in canon_rows:
            p = per.get(row[0])
            if p:
                row[11], row[12], row[13] = len(p[0]), len(p[1]), p[2]
            row[10] = 1 if row[9] and not p else 0
        s.executemany("INSERT INTO canonical_companies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", canon_rows)
        self._detail_tables(canon_of_sig, ident_sig)
        self.canon_of_sig = canon_of_sig
        return len(canon_rows)

    def _detail_tables(self, canon_of_sig, ident_sig):
        s, sigs = self.s, self.sigs
        verified = defaultdict(set)
        for r in s.execute("SELECT identity_id, evidence_type, value_key FROM identity_evidence "
                           "WHERE source_status='VERIFIED' AND evidence_type IN ('phone','email')"):
            verified[r["evidence_type"]].add((r["identity_id"], r["value_key"]))
        names, rels, addrs, phones, emails, domains, lics = (defaultdict(lambda: [set(), 0, None]) for _ in range(7))
        lic_class = {}
        for r in s.execute("SELECT identity_id, value_key, original_value FROM identity_evidence "
                           "WHERE evidence_type='license_class'"):
            lic_class[r["identity_id"]] = r["value_key"]
        for i, sg in enumerate(sigs):
            cid = canon_of_sig.get(i)
            if cid is None:
                continue
            n_ids = len(sg.identity_ids)
            stale = "STALE" if sg.historical else "CURRENT"
            for nk, typ in ((sg.name, "LEGAL" if sg.family == "roc" else ("HISTORICAL" if sg.historical else "OBSERVED")),
                            (sg.dba, "DBA")):
                if nk:
                    e = names[(cid, nk, typ)]
                    e[0].add(sg.family); e[1] += n_ids
                    e[2] = e[2] or sg.originals.most_common(1)[0][0]
            if sg.name and sg.dba:
                e = rels[(cid, sg.name, sg.dba)]
                e[0].add(sg.family); e[1] += 1
            if sg.street:
                e = addrs[(cid, sg.street, sg.zip5 or "")]
                e[0].add(sg.family); e[1] += n_ids; e[2] = sg.city if not sg.historical else e[2]
            if sg.phone:
                e = phones[(cid, sg.phone)]
                e[0].add(sg.family); e[1] += n_ids
                if any((iid, sg.phone) in verified["phone"] for iid in sg.identity_ids):
                    e[2] = "VERIFIED"
            if sg.email:
                e = emails[(cid, sg.email)]
                e[0].add(sg.family); e[1] += n_ids
                if any((iid, sg.email) in verified["email"] for iid in sg.identity_ids):
                    e[2] = "VERIFIED"
            if sg.domain:
                e = domains[(cid, sg.domain)]
                e[0].add(sg.family); e[1] += n_ids
            if sg.license and sg.family == "roc":
                e = lics[(cid, sg.license)]
                e[0].add(sg.family)
                e[2] = lic_class.get(sg.identity_ids[0])
        fam = lambda st: ",".join(sorted(st))  # noqa: E731
        s.executemany("INSERT INTO company_names VALUES (?,?,?,?,?,?)",
                      [(c, n, o, t, fam(f), k) for (c, n, t), (f, k, o) in names.items()])
        s.executemany("INSERT INTO company_name_relationships VALUES (?,?,?,?,?)",
                      [(c, a, b, "DBA_OF", fam(f)) for (c, a, b), (f, _k, _o) in rels.items()])
        sh = self.shared
        s.executemany("INSERT INTO company_addresses VALUES (?,?,?,?,?,?,?,?)",
                      [(c, st, z, city, fam(f), k, 1 if st in sh["street"] else 0, "CURRENT")
                       for (c, st, z), (f, k, city) in addrs.items()])
        s.executemany("INSERT INTO company_phones VALUES (?,?,?,?,?,?,?)",
                      [(c, p, fam(f), k, 1 if p in sh["phone"] else 0, 1 if v == "VERIFIED" else 0, "CURRENT")
                       for (c, p), (f, k, v) in phones.items()])
        s.executemany("INSERT INTO company_emails VALUES (?,?,?,?,?,?)",
                      [(c, e_, fam(f), k, 1 if v == "VERIFIED" else 0, "CURRENT") for (c, e_), (f, k, v) in emails.items()])
        s.executemany("INSERT INTO company_domains VALUES (?,?,?,?,?,?)",
                      [(c, d, fam(f), k, 1 if d in sh["domain"] else 0, "CURRENT") for (c, d), (f, k, _v) in domains.items()])
        s.executemany("INSERT INTO company_licenses VALUES (?,?,?,?,?)",
                      [(c, l, cls, fam(f), "CURRENT") for (c, l), (f, _k, cls) in lics.items()])


def resolve(shadow, cfg: ShadowConfig, batch_id: str) -> Resolver:
    r = Resolver(shadow, cfg, batch_id)
    r.load()
    r.strong_unions()
    r.name_attachment()
    r.held_person_reviews()
    as_of = date.fromisoformat(cfg.as_of) if cfg.as_of else date.today()
    r.metrics["canonical_companies"] = r.materialize(as_of)
    return r
