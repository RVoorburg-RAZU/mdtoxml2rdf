#!/usr/bin/env python3
import argparse
import os
import re
import sys
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from lxml import etree
from rdflib import Graph, Namespace, URIRef, BNode, Literal
from rdflib.namespace import RDF, RDFS, XSD

try:
    from pyshacl import validate as shacl_validate
except Exception:
    shacl_validate = None

MDTO = Namespace("http://www.nationaalarchief.nl/mdto#")
EX = Namespace("http://example.com/mdto/")

XML_NS = "https://www.nationaalarchief.nl/mdto"

# Simple slugify to keep URIs deterministic and safe
_slug_re = re.compile(r"[^a-zA-Z0-9._-]+")

def slugify(value: str, maxlen: int = 80) -> str:
    value = value.strip()
    value = value.replace(" ", "-")
    value = _slug_re.sub("-", value)
    value = re.sub(r"-+", "-", value)
    return value[:maxlen].strip("-") or "id"


def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


class UriPlanner:
    def __init__(self, base: str):
        self.base = base if base.endswith('/') else base + '/'
        # key: (class, keystr) -> uri
        self._map: Dict[Tuple[str, str], str] = {}

    def uri(self, kind: str, key: str) -> str:
        tup = (kind, key)
        if tup in self._map:
            return self._map[tup]
        # Build safe segment
        safe = slugify(key)
        # Ensure uniqueness with short hash suffix
        h = sha1(key)[:8]
        u = f"{self.base}{kind}/{safe}-{h}"
        self._map[tup] = u
        return u


# Data structures for pass 1
class Identification:
    def __init__(self, kenmerk: str, bron: str):
        self.kenmerk = kenmerk
        self.bron = bron

    def key(self) -> str:
        return f"{self.bron or 'unknown'}::{self.kenmerk}"


class TopObject:
    def __init__(self, kind: str, element: etree._Element, file: Path):
        self.kind = kind  # 'informatieobject' or 'bestand'
        self.element = element
        self.file = file
        self.identificaties: List[Identification] = []
        self.naam: Optional[str] = None


def parse_xml_file(p: Path) -> List[TopObject]:
    tree = etree.parse(str(p))
    root = tree.getroot()
    nsmap = {None: XML_NS}
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"

    tops: List[TopObject] = []
    # The schema defines root <MDTO> with either <informatieobject> or <bestand>
    for child in root:
        if child.tag == qn('informatieobject'):
            tops.append(TopObject('informatieobject', child, p))
        elif child.tag == qn('bestand'):
            tops.append(TopObject('bestand', child, p))

    # Extract basic fields for each top
    for t in tops:
        # naam
        naam_el = t.element.find(qn('naam'))
        t.naam = naam_el.text.strip() if naam_el is not None and naam_el.text else None
        # identificatie (1..*)
        for ident_el in t.element.findall(qn('identificatie')):
            kenmerk_el = ident_el.find(qn('identificatieKenmerk'))
            bron_el = ident_el.find(qn('identificatieBron'))
            kenmerk = (kenmerk_el.text or '').strip() if kenmerk_el is not None else ''
            bron = (bron_el.text or '').strip() if bron_el is not None else ''
            if kenmerk:
                t.identificaties.append(Identification(kenmerk, bron))
    return tops


def extract_verwijzing_key(verwijzing_el: etree._Element) -> Optional[str]:
    """Return a deterministic key for verwijzingGegevens contents if present."""
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    naam_el = verwijzing_el.find(qn('verwijzingNaam'))
    id_el = verwijzing_el.find(qn('verwijzingIdentificatie'))
    if id_el is not None:
        kenmerk_el = id_el.find(qn('identificatieKenmerk'))
        bron_el = id_el.find(qn('identificatieBron'))
        if kenmerk_el is not None and kenmerk_el.text:
            kenmerk = kenmerk_el.text.strip()
            bron = bron_el.text.strip() if bron_el is not None and bron_el.text else ''
            return f"{bron or 'unknown'}::{kenmerk}"
    # fallback to naam
    if naam_el is not None and naam_el.text:
        return f"naam::{naam_el.text.strip()}"
    return None


def add_identificatie_nodes(g: Graph, planner: UriPlanner, subject: URIRef, ident_elements: List[etree._Element]):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    for ident_el in ident_elements:
        kenmerk_el = ident_el.find(qn('identificatieKenmerk'))
        bron_el = ident_el.find(qn('identificatieBron'))
        kenmerk = (kenmerk_el.text or '').strip() if kenmerk_el is not None else ''
        bron = (bron_el.text or '').strip() if bron_el is not None else ''
        key = f"{bron or 'unknown'}::{kenmerk}"
        ident_uri = URIRef(planner.uri('identificatie', key))
        g.add((ident_uri, RDF.type, MDTO.IdentificatieGegevens))
        if kenmerk:
            g.add((ident_uri, MDTO.identificatieKenmerk, Literal(kenmerk)))
        if bron:
            g.add((ident_uri, MDTO.identificatieBron, Literal(bron)))
        g.add((subject, MDTO.identificatie, ident_uri))


def add_verwijzing_node(g: Graph, planner: UriPlanner, verwijzing_el: etree._Element) -> URIRef:
    key = extract_verwijzing_key(verwijzing_el) or sha1(etree.tostring(verwijzing_el, encoding='unicode'))
    u = URIRef(planner.uri('verwijzing', key))
    g.add((u, RDF.type, MDTO.VerwijzingGegevens))
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    naam_el = verwijzing_el.find(qn('verwijzingNaam'))
    if naam_el is not None and naam_el.text:
        g.add((u, MDTO.verwijzingNaam, Literal(naam_el.text.strip())))
    id_el = verwijzing_el.find(qn('verwijzingIdentificatie'))
    if id_el is not None:
        # Reuse IdentificatieGegevens helper
        # But bouw a small list with single ident element
        add_identificatie_nodes(g, planner, u, [id_el])
    return u


def parse_begrip_geg(g: Graph, planner: UriPlanner, el: etree._Element) -> URIRef:
    """Create a BegripGegevens node and return its URI."""
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    label_el = el.find(qn('begripLabel'))
    code_el = el.find(qn('begripCode'))
    lijst_el = el.find(qn('begripBegrippenlijst'))
    label = (label_el.text or '').strip() if label_el is not None else ''
    code = (code_el.text or '').strip() if code_el is not None and code_el.text else ''
    key = f"{label}|{code}"
    u = URIRef(planner.uri('begrip', key))
    g.add((u, RDF.type, MDTO.BegripGegevens))
    if label:
        g.add((u, MDTO.begripLabel, Literal(label)))
    if code:
        g.add((u, MDTO.begripCode, Literal(code)))
    if lijst_el is not None:
        verwijzing_uri = add_verwijzing_node(g, planner, lijst_el)
        g.add((u, MDTO.begripBegrippenlijst, verwijzing_uri))
    return u


def add_dekking_in_tijd(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('dekkingintijd', key))
    g.add((u, RDF.type, MDTO.DekkingInTijdGegevens))
    # type
    type_el = el.find(qn('dekkingInTijdType'))
    if type_el is not None:
        begr = parse_begrip_geg(g, planner, type_el)
        g.add((u, MDTO.dekkingInTijdType, begr))
    # begin/eind datum; infer datatype
    def emit_date(pred, text: str):
        text = text.strip()
        dt = None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = XSD.date
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            dt = XSD.gYearMonth
        elif re.fullmatch(r"\d{4}", text):
            dt = XSD.gYear
        else:
            # fallback: try date
            dt = XSD.date
        g.add((u, pred, Literal(text, datatype=dt)))
    beg_el = el.find(qn('dekkingInTijdBegindatum'))
    if beg_el is not None and beg_el.text:
        emit_date(MDTO.dekkingInTijdBeginDatum, beg_el.text)
    eind_el = el.find(qn('dekkingInTijdEinddatum'))
    if eind_el is not None and eind_el.text:
        emit_date(MDTO.dekkingInTijdEindDatum, eind_el.text)
    g.add((parent, MDTO.dekkingInTijd, u))


def add_event(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('event', key))
    g.add((u, RDF.type, MDTO.EventGegevens))
    type_el = el.find(qn('eventType'))
    if type_el is not None:
        begr = parse_begrip_geg(g, planner, type_el)
        g.add((u, MDTO.eventType, begr))
    tijd_el = el.find(qn('eventTijd'))
    if tijd_el is not None and tijd_el.text:
        text = tijd_el.text.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text):
            dt = XSD.dateTime
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = XSD.date
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            dt = XSD.gYearMonth
        elif re.fullmatch(r"\d{4}", text):
            dt = XSD.gYear
        else:
            dt = XSD.dateTime
        g.add((u, MDTO.eventTijd, Literal(text, datatype=dt)))
    verant_el = el.find(qn('eventVerantwoordelijkeActor'))
    if verant_el is not None:
        vuri = add_verwijzing_node(g, planner, verant_el)
        g.add((u, MDTO.eventVerantwoordelijkeActor, vuri))
    res_el = el.find(qn('eventResultaat'))
    if res_el is not None and res_el.text:
        g.add((u, MDTO.eventResultaat, Literal(res_el.text.strip())))
    g.add((parent, MDTO.event, u))


def add_termijn(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element, pred: URIRef):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('termijn', key))
    g.add((u, RDF.type, MDTO.TermijnGegevens))
    trig_el = el.find(qn('termijnTriggerStartLooptijd'))
    if trig_el is not None:
        begr = parse_begrip_geg(g, planner, trig_el)
        g.add((u, MDTO.termijnTriggerStartLooptijd, begr))
    start_el = el.find(qn('termijnStartdatumLooptijd'))
    if start_el is not None and start_el.text:
        g.add((u, MDTO.termijnStartdatumLooptijd, Literal(start_el.text.strip(), datatype=XSD.date)))
    duur_el = el.find(qn('termijnLooptijd'))
    if duur_el is not None and duur_el.text:
        g.add((u, MDTO.termijnLooptijd, Literal(duur_el.text.strip(), datatype=XSD.duration)))
    eind_el = el.find(qn('termijnEinddatum'))
    if eind_el is not None and eind_el.text:
        text = eind_el.text.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = XSD.date
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            dt = XSD.gYearMonth
        elif re.fullmatch(r"\d{4}", text):
            dt = XSD.gYear
        else:
            dt = XSD.date
        g.add((u, MDTO.termijnEinddatum, Literal(text, datatype=dt)))
    g.add((parent, pred, u))


def add_checksum(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('checksum', key))
    g.add((u, RDF.type, MDTO.ChecksumGegevens))
    alg_el = el.find(qn('checksumAlgoritme'))
    if alg_el is not None:
        begr = parse_begrip_geg(g, planner, alg_el)
        g.add((u, MDTO.checksumAlgoritme, begr))
    w_el = el.find(qn('checksumWaarde'))
    if w_el is not None and w_el.text:
        g.add((u, MDTO.checksumWaarde, Literal(w_el.text.strip())))
    d_el = el.find(qn('checksumDatum'))
    if d_el is not None and d_el.text:
        g.add((u, MDTO.checksumDatum, Literal(d_el.text.strip(), datatype=XSD.dateTime)))
    g.add((parent, MDTO.checksum, u))


def add_raadpleeglocatie(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('raadpleeglocatie', key))
    g.add((u, RDF.type, MDTO.RaadpleeglocatieGegevens))
    # fysiek (0..*)
    for fys_el in el.findall(qn('raadpleeglocatieFysiek')):
        vuri = add_verwijzing_node(g, planner, fys_el)
        g.add((u, MDTO.raadpleeglocatieFysiek, vuri))
    # online (0..*)
    for onl_el in el.findall(qn('raadpleeglocatieOnline')):
        if onl_el.text:
            g.add((u, MDTO.raadpleeglocatieOnline, Literal(onl_el.text.strip(), datatype=XSD.anyURI)))
    g.add((parent, MDTO.raadpleeglocatie, u))


def add_betrokkene(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('betrokkene', key))
    g.add((u, RDF.type, MDTO.BetrokkeneGegevens))
    typ_el = el.find(qn('betrokkeneTypeRelatie'))
    if typ_el is not None:
        begr = parse_begrip_geg(g, planner, typ_el)
        g.add((u, MDTO.betrokkeneTypeRelatie, begr))
    act_el = el.find(qn('betrokkeneActor'))
    if act_el is not None:
        vuri = add_verwijzing_node(g, planner, act_el)
        g.add((u, MDTO.betrokkeneActor, vuri))
    g.add((parent, MDTO.betrokkene, u))


def add_beperking_gebruik(g: Graph, planner: UriPlanner, parent: URIRef, el: etree._Element):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    key = sha1(etree.tostring(el, encoding='unicode'))
    u = URIRef(planner.uri('beperking', key))
    g.add((u, RDF.type, MDTO.BeperkingGebruikGegevens))
    typ_el = el.find(qn('beperkingGebruikType'))
    if typ_el is not None:
        begr = parse_begrip_geg(g, planner, typ_el)
        g.add((u, MDTO.beperkingGebruikType, begr))
    nad_el = el.find(qn('beperkingGebruikNadereBeschrijving'))
    if nad_el is not None and nad_el.text:
        g.add((u, MDTO.beperkingGebruikNadereBeschrijving, Literal(nad_el.text.strip())))
    for doc_el in el.findall(qn('beperkingGebruikDocumentatie')):
        vuri = add_verwijzing_node(g, planner, doc_el)
        g.add((u, MDTO.beperkingGebruikDocumentatie, vuri))
    term_el = el.find(qn('beperkingGebruikTermijn'))
    if term_el is not None:
        add_termijn(g, planner, u, term_el, MDTO.beperkingGebruikTermijn)
    g.add((parent, MDTO.beperkingGebruik, u))


def convert_informatieobject(
    g: Graph,
    planner: UriPlanner,
    el: etree._Element,
    planned_uri: URIRef,
    object_index: Dict[str, Tuple[URIRef, str]],
):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    s = planned_uri
    g.add((s, RDF.type, MDTO.Informatieobject))
    # naam
    naam_el = el.find(qn('naam'))
    if naam_el is not None and naam_el.text:
        g.add((s, MDTO.naam, Literal(naam_el.text.strip())))
    # identificatie
    add_identificatie_nodes(g, planner, s, el.findall(qn('identificatie')))
    # aggregatieniveau
    an_el = el.find(qn('aggregatieniveau'))
    if an_el is not None:
        uri = parse_begrip_geg(g, planner, an_el)
        g.add((s, MDTO.aggregatieniveau, uri))
    # classificatie (0..*)
    for cls_el in el.findall(qn('classificatie')):
        uri = parse_begrip_geg(g, planner, cls_el)
        g.add((s, MDTO.classificatie, uri))
    # trefwoord, omschrijving, taal
    for tref_el in el.findall(qn('trefwoord')):
        if tref_el.text:
            g.add((s, MDTO.trefwoord, Literal(tref_el.text.strip())))
    for oms_el in el.findall(qn('omschrijving')):
        if oms_el.text:
            g.add((s, MDTO.omschrijving, Literal(oms_el.text.strip())))
    for taal_el in el.findall(qn('taal')):
        if taal_el.text:
            g.add((s, MDTO.taal, Literal(taal_el.text.strip(), datatype=XSD.language)))
    # raadpleeglocatie
    for rl_el in el.findall(qn('raadpleeglocatie')):
        add_raadpleeglocatie(g, planner, s, rl_el)
    # dekkingInTijd (0..*)
    for dit_el in el.findall(qn('dekkingInTijd')):
        add_dekking_in_tijd(g, planner, s, dit_el)
    # dekkingInRuimte (0..*)
    for dir_el in el.findall(qn('dekkingInRuimte')):
        vuri = add_verwijzing_node(g, planner, dir_el)
        g.add((s, MDTO.dekkingInRuimte, vuri))
    # event (0..*)
    for ev_el in el.findall(qn('event')):
        add_event(g, planner, s, ev_el)
    # waardering (1..1)
    w_el = el.find(qn('waardering'))
    if w_el is not None:
        uri = parse_begrip_geg(g, planner, w_el)
        g.add((s, MDTO.waardering, uri))
    # bewaartermijn (0..1)
    bt_el = el.find(qn('bewaartermijn'))
    if bt_el is not None:
        add_termijn(g, planner, s, bt_el, MDTO.bewaartermijn)
    # informatiecategorie (0..1)
    ic_el = el.find(qn('informatiecategorie'))
    if ic_el is not None:
        uri = parse_begrip_geg(g, planner, ic_el)
        g.add((s, MDTO.informatiecategorie, uri))
    # isOnderdeelVan (0..*), must point to Informatieobject per SHACL
    for iov_el in el.findall(qn('isOnderdeelVan')):
        key = extract_verwijzing_key(iov_el)
        if key and key in object_index and object_index[key][1] == 'informatieobject':
            g.add((s, MDTO.isOnderdeelVan, object_index[key][0]))
        # else: skip to avoid SHACL violation
    # bevatOnderdeel (0..*), must point to Informatieobject per SHACL
    for bov_el in el.findall(qn('bevatOnderdeel')):
        key = extract_verwijzing_key(bov_el)
        if key and key in object_index and object_index[key][1] == 'informatieobject':
            g.add((s, MDTO.bevatOnderdeel, object_index[key][0]))
        # else: skip
    # heeftRepresentatie (0..*), must point to Bestand per SHACL
    for hr_el in el.findall(qn('heeftRepresentatie')):
        key = extract_verwijzing_key(hr_el)
        if key and key in object_index and object_index[key][1] == 'bestand':
            g.add((s, MDTO.heeftRepresentatie, object_index[key][0]))
        # else: skip
    # aanvullendeMetagegevens (0..*), must point to Bestand per SHACL
    for am_el in el.findall(qn('aanvullendeMetagegevens')):
        key = extract_verwijzing_key(am_el)
        if key and key in object_index and object_index[key][1] == 'bestand':
            g.add((s, MDTO.aanvullendeMetagegevens, object_index[key][0]))
        # else: skip
    # gerelateerdInformatieobject (0..*)
    for gio_el in el.findall(qn('gerelateerdInformatieobject')):
        key = sha1(etree.tostring(gio_el, encoding='unicode'))
        u = URIRef(planner.uri('gerelateerd', key))
        g.add((u, RDF.type, MDTO.GerelateerdInformatieobjectGegevens))
        vv_el = gio_el.find(qn('gerelateerdInformatieobjectVerwijzing'))
        if vv_el is not None:
            vuri = add_verwijzing_node(g, planner, vv_el)
            g.add((u, MDTO.gerelateerdInformatieobjectVerwijzing, vuri))
        tr_el = gio_el.find(qn('gerelateerdInformatieobjectTypeRelatie'))
        if tr_el is not None:
            begr = parse_begrip_geg(g, planner, tr_el)
            g.add((u, MDTO.gerelateerdInformatieobjectTypeRelatie, begr))
        g.add((s, MDTO.gerelateerdInformatieobject, u))
    # archiefvormer (1..*)
    for av_el in el.findall(qn('archiefvormer')):
        vuri = add_verwijzing_node(g, planner, av_el)
        g.add((s, MDTO.archiefvormer, vuri))
    # betrokkene (0..*)
    for bet_el in el.findall(qn('betrokkene')):
        add_betrokkene(g, planner, s, bet_el)
    # activiteit (0..1)
    act_el = el.find(qn('activiteit'))
    if act_el is not None:
        vuri = add_verwijzing_node(g, planner, act_el)
        g.add((s, MDTO.activiteit, vuri))
    # beperkingGebruik (1..*) in XML
    for bg_el in el.findall(qn('beperkingGebruik')):
        add_beperking_gebruik(g, planner, s, bg_el)


def convert_bestand(
    g: Graph,
    planner: UriPlanner,
    el: etree._Element,
    planned_uri: URIRef,
    object_index: Dict[str, Tuple[URIRef, str]],
):
    def qn(local: str) -> str:
        return f"{{{XML_NS}}}{local}"
    s = planned_uri
    g.add((s, RDF.type, MDTO.Bestand))
    # naam
    naam_el = el.find(qn('naam'))
    if naam_el is not None and naam_el.text:
        g.add((s, MDTO.naam, Literal(naam_el.text.strip())))
    # identificatie
    add_identificatie_nodes(g, planner, s, el.findall(qn('identificatie')))
    # omvang (required)
    omv_el = el.find(qn('omvang'))
    if omv_el is not None and omv_el.text:
        g.add((s, MDTO.omvang, Literal(int(omv_el.text.strip()), datatype=XSD.integer)))
    # bestandsformaat (required)
    bf_el = el.find(qn('bestandsformaat'))
    if bf_el is not None:
        uri = parse_begrip_geg(g, planner, bf_el)
        g.add((s, MDTO.bestandsformaat, uri))
    # checksum (1..*)
    for cs_el in el.findall(qn('checksum')):
        add_checksum(g, planner, s, cs_el)
    # URLBestand (0..1)
    url_el = el.find(qn('URLBestand'))
    if url_el is not None and url_el.text:
        g.add((s, MDTO.URLBestand, Literal(url_el.text.strip(), datatype=XSD.anyURI)))
    # isRepresentatieVan (required 1..1 in XML, SHACL expects Informatieobject)
    irv_el = el.find(qn('isRepresentatieVan'))
    if irv_el is not None:
        # Try resolve to informatieobject by verwijzingIdentificatie
        key = extract_verwijzing_key(irv_el)
        if key and key in object_index and object_index[key][1] == 'informatieobject':
            g.add((s, MDTO.isRepresentatieVan, object_index[key][0]))
        else:
            # SHACL expects Informatieobject; if unresolved, still emit verwijzing but validation may fail
            vuri = add_verwijzing_node(g, planner, irv_el)
            g.add((s, MDTO.isRepresentatieVan, vuri))


def build_index_for_resolution(tops: List[TopObject]) -> Dict[str, Tuple[URIRef, str]]:
    """Map identification key -> (planned URI, kind) for resolvable objects (informatieobject/bestand)."""
    idx: Dict[str, Tuple[URIRef, str]] = {}
    # Note: we map by first identification key
    for t in tops:
        if not t.identificaties:
            continue
        key = t.identificaties[0].key()
        idx[key] = (URIRef(""), t.kind)  # placeholder; actual URI will be filled later in main
    return idx


def main():
    p = argparse.ArgumentParser(description="Convert MDTO XML to RDF Turtle with SHACL validation.")
    p.add_argument("--xml-dir", default="xml/example", help="Directory containing MDTO XML files")
    p.add_argument("--out", default="output/mdto.ttl", help="Output TTL file")
    p.add_argument("--base", default=str(EX), help="Base URI for generated resources")
    p.add_argument("--validate", action="store_true", help="Validate output with SHACL shapes")
    p.add_argument("--shapes", default="rdf/mdto-rdf-1.0-shacl.ttl", help="Path to SHACL shapes TTL")
    args = p.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        print(f"XML directory not found: {xml_dir}", file=sys.stderr)
        sys.exit(2)

    planner = UriPlanner(args.base)

    # Pass 1: parse and index
    tops: List[TopObject] = []
    for f in sorted(xml_dir.glob("**/*.xml")):
        try:
            tops.extend(parse_xml_file(f))
        except Exception as e:
            print(f"Failed to parse {f}: {e}", file=sys.stderr)
            sys.exit(2)

    # Build resolution index and allocate URIs
    object_index: Dict[str, Tuple[URIRef, str]] = {}
    for t in tops:
        # choose key from first identificatie if present; else fallback to file+name
        if t.identificaties:
            key = t.identificaties[0].key()
        else:
            key = f"file::{t.file.name}|{t.kind}|{t.naam or ''}"
        uri = URIRef(planner.uri(t.kind, key))
        object_index[key] = (uri, t.kind)

    # Pass 2: build RDF
    g = Graph()
    g.bind("mdto", MDTO)
    g.bind("xsd", XSD)

    for t in tops:
        # Obtain planned uri again consistently
        if t.identificaties:
            key = t.identificaties[0].key()
        else:
            key = f"file::{t.file.name}|{t.kind}|{t.naam or ''}"
        subj = object_index[key][0]
        if t.kind == 'informatieobject':
            convert_informatieobject(g, planner, t.element, subj, object_index)
        elif t.kind == 'bestand':
            convert_bestand(g, planner, t.element, subj, object_index)

    # Ensure output directory exists
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(out_path), format='turtle')
    print(f"Wrote RDF to {out_path}")

    if args.validate:
        if shacl_validate is None:
            print("pySHACL is not installed; cannot validate.", file=sys.stderr)
            sys.exit(3)
        shapes_path = Path(args.shapes)
        if not shapes_path.exists():
            print(f"Shapes file not found: {shapes_path}", file=sys.stderr)
            sys.exit(3)
        data_graph = Graph().parse(str(out_path), format='turtle')
        shapes_graph = Graph().parse(str(shapes_path), format='turtle')
        conforms, report_graph, report_text = shacl_validate(
            data_graph=data_graph,
            shacl_graph=shapes_graph,
            inference='rdfs',
            abort_on_first=False,
            allow_infos=True,
            allow_warnings=True,
        )
        rep_path = out_path.with_suffix('.shacl-report.ttl')
        report_graph.serialize(destination=str(rep_path), format='turtle')
        print(report_text)
        print(f"SHACL report written to {rep_path}")
        if not conforms:
            print("SHACL validation did not conform.", file=sys.stderr)
            sys.exit(4)


if __name__ == "__main__":
    main()
