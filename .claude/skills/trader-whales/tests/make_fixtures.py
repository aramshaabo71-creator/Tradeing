#!/usr/bin/env python3
"""Writes the offline fixtures in tests/fixtures. Made-up companies and people,
shaped exactly like SEC EDGAR's real files (daily form index, full submission
.txt with the XML Schedule 13D/13G and Form 4, submissions JSON, 13F folders).

    python3 tests/make_fixtures.py
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "fixtures")

ACME, BETA, GAMMA = "0002222222", "0004444444", "0006666666"
ACTIVIST, INDEX, OLDFUND, FUND13F = "0001111111", "0003333333", "0007777777", "0005555555"


def put(name, data):
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data, indent=1))


def arch(url_path):
    return url_path.strip("/").replace("/", "_")


def header(acc, form, filed, blocks):
    out = ["<SEC-DOCUMENT>%s.txt : %s" % (acc, filed), "<SEC-HEADER>%s.hdr.sgml : %s" % (acc, filed),
           "ACCESSION NUMBER:\t\t%s" % acc, "CONFORMED SUBMISSION TYPE:\t%s" % form,
           "PUBLIC DOCUMENT COUNT:\t\t1", "FILED AS OF DATE:\t\t%s" % filed, ""]
    for role, cik, name in blocks:
        out += ["%s:\t" % role, "", "\tCOMPANY DATA:\t",
                "\t\tCOMPANY CONFORMED NAME:\t\t\t%s" % name,
                "\t\tCENTRAL INDEX KEY:\t\t\t%s" % cik, ""]
    out.append("</SEC-HEADER>")
    return "\n".join(out)


def schedule13d(acc, form, filed, issuer, iname, filer, fname, pct, shares, event):
    xml = """<?xml version="1.0" encoding="UTF-8"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D" xmlns:common="http://www.sec.gov/edgar/common">
  <headerData><submissionType>%(form)s</submissionType></headerData>
  <formData>
    <coverPageHeader>
      <securitiesClassTitle>Common Stock</securitiesClassTitle>
      <dateOfEvent>%(event)s</dateOfEvent>
      <issuerInfo><issuerCIK>%(issuer)s</issuerCIK>
        <issuerCusips><issuerCusipNumber>000000000</issuerCusipNumber></issuerCusips>
        <issuerName>%(iname)s</issuerName></issuerInfo>
    </coverPageHeader>
    <reportingPersons>
      <reportingPersonInfo>
        <reportingPersonCIK>%(filer)s</reportingPersonCIK>
        <reportingPersonName>%(fname)s</reportingPersonName>
        <aggregateAmountOwned>%(shares)s</aggregateAmountOwned>
        <percentOfClass>%(pct)s</percentOfClass>
      </reportingPersonInfo>
    </reportingPersons>
    <items1To7><item4><transactionPurpose>The Reporting Person intends to engage with the board about strategy.</transactionPurpose></item4></items1To7>
  </formData>
</edgarSubmission>""" % locals()
    return (header(acc, form, filed, [("SUBJECT COMPANY", issuer, iname), ("FILED BY", filer, fname)])
            + "\n<DOCUMENT>\n<TYPE>%s\n<SEQUENCE>1\n<FILENAME>primary_doc.xml\n<TEXT>\n<XML>\n%s\n</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n"
            % (form, xml))


def schedule13g(acc, form, filed, issuer, iname, filer, fname, pct, shares, event, five_or_less):
    xml = """<?xml version="1.0" encoding="UTF-8"?><edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g" xmlns:com="http://www.sec.gov/edgar/common">
  <headerData><submissionType>%(form)s</submissionType></headerData>
  <formData>
    <coverPageHeader>
      <amendmentNo>3</amendmentNo>
      <securitiesClassTitle>Common Stock</securitiesClassTitle>
      <eventDateRequiresFilingThisStatement>%(event)s</eventDateRequiresFilingThisStatement>
      <issuerInfo><issuerCik>%(issuer)s</issuerCik><issuerName>%(iname)s</issuerName>
        <issuerCusips><issuerCusipNumber>000000000</issuerCusipNumber></issuerCusips></issuerInfo>
    </coverPageHeader>
    <coverPageHeaderReportingPersonDetails>
      <reportingPersonName>%(fname)s</reportingPersonName>
      <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>%(shares)s</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
      <classPercent>%(pct)s</classPercent>
    </coverPageHeaderReportingPersonDetails>
    <items><item5><notApplicableFlag>%(na)s</notApplicableFlag><classOwnership5PercentOrLess>%(five)s</classOwnership5PercentOrLess></item5></items>
  </formData>
</edgarSubmission>""" % dict(locals(), na="N" if five_or_less else "Y", five="Y" if five_or_less else "N")
    return (header(acc, form, filed, [("SUBJECT COMPANY", issuer, iname), ("FILED BY", filer, fname)])
            + "\n<DOCUMENT>\n<TYPE>%s\n<SEQUENCE>1\n<FILENAME>primary_doc.xml\n<TEXT>\n<XML>\n%s\n</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n"
            % (form, xml))


def old_text_13g(acc, filed, issuer, iname, filer, fname):
    body = """<HTML><BODY><P>SCHEDULE 13G</P>
<TABLE><TR><TD>9</TD><TD>AGGREGATE AMOUNT BENEFICIALLY OWNED BY EACH REPORTING PERSON</TD><TD>3,050,000</TD></TR>
<TR><TD>11</TD><TD>PERCENT OF CLASS REPRESENTED BY AMOUNT IN ROW (9)</TD><TD>6.1%</TD></TR></TABLE>
</BODY></HTML>"""
    return (header(acc, "SC 13G", filed, [("SUBJECT COMPANY", issuer, iname), ("FILED BY", filer, fname)])
            + "\n<DOCUMENT>\n<TYPE>SC 13G\n<SEQUENCE>1\n<FILENAME>d1.htm\n<TEXT>\n%s\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n" % body)


def form4(acc, filed, issuer, iname, sym, owner, oname, rel, trades, plan=False, footnotes=()):
    rels = "".join("<%s>%s</%s>" % (k, v, k) for k, v in rel.items())
    tx = ""
    for t in trades:
        fid = "".join('<footnoteId id="%s"/>' % f for f in t.get("fn", []))
        tx += """<nonDerivativeTransaction>
 <securityTitle><value>Common Stock</value></securityTitle>
 <transactionDate><value>%(date)s</value></transactionDate>
 <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>%(code)s</transactionCode>%(fid)s</transactionCoding>
 <transactionAmounts>
  <transactionShares><value>%(shares)s</value></transactionShares>
  <transactionPricePerShare><value>%(price)s</value></transactionPricePerShare>
  <transactionAcquiredDisposedCode><value>%(ad)s</value></transactionAcquiredDisposedCode>
 </transactionAmounts>
 <postTransactionAmounts><sharesOwnedFollowingTransaction><value>%(after)s</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
 <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
</nonDerivativeTransaction>
""" % dict(t, fid=fid)
    fns = "".join('<footnote id="%s">%s</footnote>' % f for f in footnotes)
    xml = """<?xml version="1.0"?>
<ownershipDocument>
 <schemaVersion>X0609</schemaVersion><documentType>4</documentType>
 <issuer><issuerCik>%s</issuerCik><issuerName>%s</issuerName><issuerTradingSymbol>%s</issuerTradingSymbol></issuer>
 <reportingOwner><reportingOwnerId><rptOwnerCik>%s</rptOwnerCik><rptOwnerName>%s</rptOwnerName></reportingOwnerId>
  <reportingOwnerRelationship>%s</reportingOwnerRelationship></reportingOwner>
 <aff10b5One>%s</aff10b5One>
 <nonDerivativeTable>%s</nonDerivativeTable>
 <footnotes>%s</footnotes>
</ownershipDocument>""" % (issuer, iname, sym, owner, oname, rels, "true" if plan else "false", tx, fns)
    return (header(acc, "4", filed, [("REPORTING-OWNER", owner, oname), ("ISSUER", issuer, iname)])
            + "\n<DOCUMENT>\n<TYPE>4\n<SEQUENCE>1\n<FILENAME>form4.xml\n<TEXT>\n<XML>\n%s\n</XML>\n</TEXT>\n</DOCUMENT>\n</SEC-DOCUMENT>\n" % xml)


def idx_line(form, name, cik, date, acc):
    return "%-17s%-62s%-12s%-12s%s" % (form, name, int(cik), date,
                                        "edgar/data/%d/%s.txt" % (int(cik), acc))


def full_txt(cik, acc, text):
    """EDGAR serves a filing under every CIK it belongs to (company and filers)."""
    import re
    ciks = {int(cik)} | {int(c) for c in re.findall(r"CENTRAL INDEX KEY:\s*(\d+)", text)}
    for c in ciks:
        put(arch("Archives/edgar/data/%d/%s.txt" % (c, acc)), text)


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))

    put("files_company_tickers.json", {
        "0": {"cik_str": int(ACME), "ticker": "ACME", "title": "Acme Robotics Inc"},
        "1": {"cik_str": int(BETA), "ticker": "BETA", "title": "Beta Foods Corp"},
        "2": {"cik_str": int(GAMMA), "ticker": "GAMA", "title": "Gamma Mining Co"},
    })

    # ---- daily index: 2026-11-03 and 2026-11-04 exist, 2026-11-05 not yet
    put(arch("Archives/edgar/daily-index/2026/QTR4/index.json"), {"directory": {"item": [
        {"name": "form.20261103.idx"}, {"name": "form.20261104.idx"}, {"name": "master.20261104.idx"}]}})
    head = ("Description:           Daily Index of EDGAR Dissemination Feed by Form Type\n\n"
            "Form Type   Company Name                                                  CIK         Date Filed  File Name\n"
            + "-" * 140 + "\n")
    d1 = [
        idx_line("SCHEDULE 13D", "Acme Robotics Inc", ACME, "20261103", "0001111111-26-000010"),
        idx_line("SCHEDULE 13D", "Activist Partners LP", ACTIVIST, "20261103", "0001111111-26-000010"),
        idx_line("SCHEDULE 13G/A", "Acme Robotics Inc", ACME, "20261103", "0003333333-26-000020"),
        idx_line("SCHEDULE 13G/A", "Big Index Advisors", INDEX, "20261103", "0003333333-26-000020"),
        idx_line("SC 13G", "Acme Robotics Inc", ACME, "20261103", "0007777777-26-000030"),
        idx_line("SC 13G", "Old Fund LLC", OLDFUND, "20261103", "0007777777-26-000030"),
        idx_line("4", "Acme Robotics Inc", ACME, "20261103", "0009000001-26-000001"),
        idx_line("4", "Ray Lena", "0009000001", "20261103", "0009000001-26-000001"),
        idx_line("8-K", "Acme Robotics Inc", ACME, "20261103", "0002222222-26-000099"),
        idx_line("4", "Gamma Mining Co", GAMMA, "20261103", "0009000009-26-000001"),
    ]
    d2 = [
        idx_line("SCHEDULE 13D/A", "Beta Foods Corp", BETA, "20261104", "0001111111-26-000011"),
        idx_line("SCHEDULE 13D/A", "Activist Partners LP", ACTIVIST, "20261104", "0001111111-26-000011"),
        idx_line("SCHEDULE 13G/A", "Beta Foods Corp", BETA, "20261104", "0003333333-26-000021"),
        idx_line("SCHEDULE 13G/A", "Big Index Advisors", INDEX, "20261104", "0003333333-26-000021"),
        idx_line("4", "Acme Robotics Inc", ACME, "20261104", "0009000002-26-000001"),
        idx_line("4", "Stone Kim", "0009000002", "20261104", "0009000002-26-000001"),
        idx_line("4", "Acme Robotics Inc", ACME, "20261104", "0009000006-26-000001"),
        idx_line("4", "Holding Vehicle LLC", "0009000006", "20261104", "0009000006-26-000001"),
        idx_line("4", "Beta Foods Corp", BETA, "20261104", "0009000003-26-000001"),
        idx_line("4", "Cole Pat", "0009000003", "20261104", "0009000003-26-000001"),
        idx_line("4", "Beta Foods Corp", BETA, "20261104", "0009000004-26-000001"),
        idx_line("4", "Ng Sam", "0009000004", "20261104", "0009000004-26-000001"),
        idx_line("4", "Beta Foods Corp", BETA, "20261104", "0009000005-26-000001"),
        idx_line("4", "Diaz Ana", "0009000005", "20261104", "0009000005-26-000001"),
    ]
    put(arch("Archives/edgar/daily-index/2026/QTR4/form.20261103.idx"), head + "\n".join(d1) + "\n")
    put(arch("Archives/edgar/daily-index/2026/QTR4/form.20261104.idx"), head + "\n".join(d2) + "\n")

    # ---- 13D / 13G
    full_txt(ACME, "0001111111-26-000010", schedule13d(
        "0001111111-26-000010", "SCHEDULE 13D", "20261103", ACME, "Acme Robotics Inc",
        ACTIVIST, "Activist Partners LP", "7.5", "3750000", "10/28/2026"))
    full_txt(ACME, "0003333333-26-000020", schedule13g(
        "0003333333-26-000020", "SCHEDULE 13G/A", "20261103", ACME, "Acme Robotics Inc",
        INDEX, "Big Index Advisors", "4.2", "2100000", "09/30/2026", True))
    full_txt(ACME, "0007777777-26-000030", old_text_13g(
        "0007777777-26-000030", "20261103", ACME, "Acme Robotics Inc", OLDFUND, "Old Fund LLC"))
    full_txt(BETA, "0001111111-26-000011", schedule13d(
        "0001111111-26-000011", "SCHEDULE 13D/A", "20261104", BETA, "Beta Foods Corp",
        ACTIVIST, "Activist Partners LP", "12.0", "6000000", "11/02/2026"))
    full_txt(BETA, "0003333333-26-000021", schedule13g(
        "0003333333-26-000021", "SCHEDULE 13G/A", "20261104", BETA, "Beta Foods Corp",
        INDEX, "Big Index Advisors", "8.0", "4000000", "09/30/2026", False))
    # Beta's earlier 13D by the same activist (found through Beta's submissions list)
    full_txt(BETA, "0001111111-26-000005", schedule13d(
        "0001111111-26-000005", "SCHEDULE 13D", "20260901", BETA, "Beta Foods Corp",
        ACTIVIST, "Activist Partners LP", "9.0", "4500000", "08/25/2026"))

    # ---- Form 4
    ceo = {"isDirector": "1", "isOfficer": "1", "officerTitle": "Chief Executive Officer"}
    director = {"isDirector": "1"}
    full_txt(ACME, "0009000001-26-000001", form4(
        "0009000001-26-000001", "20261103", ACME, "Acme Robotics Inc", "ACME", "0009000001",
        "Ray Lena", ceo, [dict(date="2026-11-02", code="P", shares="100000", price="15.00",
                                ad="A", after="400000")]))
    full_txt(ACME, "0009000002-26-000001", form4(
        "0009000002-26-000001", "20261104", ACME, "Acme Robotics Inc", "ACME", "0009000002",
        "Stone Kim", director, [dict(date="2026-11-03", code="P", shares="10000", price="15.10",
                                      ad="A", after="30000")]))
    # Joint filer: the CEO's holding company reports the very same buy
    full_txt(ACME, "0009000006-26-000001", form4(
        "0009000006-26-000001", "20261104", ACME, "Acme Robotics Inc", "ACME", "0009000006",
        "Holding Vehicle LLC", {"isTenPercentOwner": "1"},
        [dict(date="2026-11-02", code="P", shares="100000", price="15.00", ad="A", after="400000")]))
    full_txt(BETA, "0009000003-26-000001", form4(
        "0009000003-26-000001", "20261104", BETA, "Beta Foods Corp", "BETA", "0009000003",
        "Cole Pat", {"isOfficer": "1", "officerTitle": "CFO"},
        [dict(date="2026-11-02", code="S", shares="200000", price="20.00", ad="D", after="50000")]))
    full_txt(BETA, "0009000004-26-000001", form4(
        "0009000004-26-000001", "20261104", BETA, "Beta Foods Corp", "BETA", "0009000004",
        "Ng Sam", ceo,
        [dict(date="2026-11-02", code="S", shares="300000", price="20.00", ad="D", after="10000",
              fn=["F1"])],
        footnotes=[("F1", "Sold under a Rule 10b5-1 trading plan adopted May 1, 2026.")]))
    full_txt(BETA, "0009000005-26-000001", form4(
        "0009000005-26-000001", "20261104", BETA, "Beta Foods Corp", "BETA", "0009000005",
        "Diaz Ana", director,
        [dict(date="2026-11-03", code="M", shares="100000", price="5.00", ad="A", after="100000"),
         dict(date="2026-11-03", code="S", shares="100000", price="21.00", ad="D", after="0"),
         dict(date="2026-11-03", code="F", shares="1000", price="21.00", ad="D", after="0")]))
    full_txt(GAMMA, "0009000009-26-000001", form4(
        "0009000009-26-000001", "20261103", GAMMA, "Gamma Mining Co", "GAMA", "0009000009",
        "Plan Seller", director,
        [dict(date="2026-11-02", code="S", shares="500000", price="10.00", ad="D", after="0")],
        plan=True))

    # ---- submissions lists
    def subs(cik, name, rows):
        rec = {k: [] for k in ("accessionNumber", "filingDate", "reportDate", "form",
                               "primaryDocument")}
        for acc, date, form, period, doc in rows:
            rec["accessionNumber"].append(acc)
            rec["filingDate"].append(date)
            rec["reportDate"].append(period)
            rec["form"].append(form)
            rec["primaryDocument"].append(doc)
        put("submissions_CIK%s.json" % cik, {"cik": cik, "name": name,
                                              "filings": {"recent": rec}})

    subs(BETA, "Beta Foods Corp", [
        ("0009000005-26-000001", "2026-11-04", "4", "", "form4.xml"),
        ("0009000004-26-000001", "2026-11-04", "4", "", "form4.xml"),
        ("0009000003-26-000001", "2026-11-04", "4", "", "form4.xml"),
        ("0003333333-26-000021", "2026-11-04", "SCHEDULE 13G/A", "", "primary_doc.xml"),
        ("0001111111-26-000011", "2026-11-04", "SCHEDULE 13D/A", "", "primary_doc.xml"),
        ("0001111111-26-000005", "2026-09-01", "SCHEDULE 13D", "", "primary_doc.xml"),
    ])
    subs(ACME, "Acme Robotics Inc", [
        ("0009000006-26-000001", "2026-11-04", "4", "", "form4.xml"),
        ("0009000002-26-000001", "2026-11-04", "4", "", "form4.xml"),
        ("0009000001-26-000001", "2026-11-03", "4", "", "form4.xml"),
        ("0007777777-26-000030", "2026-11-03", "SC 13G", "", "d1.htm"),
        ("0003333333-26-000020", "2026-11-03", "SCHEDULE 13G/A", "", "primary_doc.xml"),
        ("0001111111-26-000010", "2026-11-03", "SCHEDULE 13D", "", "primary_doc.xml"),
        ("0002222222-26-000099", "2026-11-03", "8-K", "", "a.htm"),
    ])
    subs(ACTIVIST, "Activist Partners LP", [
        ("0001111111-26-000011", "2026-11-04", "SCHEDULE 13D/A", "", "primary_doc.xml"),
        ("0001111111-26-000010", "2026-11-03", "SCHEDULE 13D", "", "primary_doc.xml"),
        ("0001111111-26-000005", "2026-09-01", "SCHEDULE 13D", "", "primary_doc.xml"),
    ])

    # ---- 13F: one fund, Q3 filed in the window, Q2 before
    q3, q2 = "0005555555-26-000002", "0005555555-26-000001"
    subs(FUND13F, "Whale Capital LP", [
        (q3, "2026-11-04", "13F-HR", "2026-09-30", "primary_doc.xml"),
        (q2, "2026-08-14", "13F-HR", "2026-06-30", "primary_doc.xml"),
    ])
    subs(ACTIVIST, "Activist Partners LP", [
        ("0001111111-26-000011", "2026-11-04", "SCHEDULE 13D/A", "", "primary_doc.xml"),
        ("0001111111-26-000010", "2026-11-03", "SCHEDULE 13D", "", "primary_doc.xml"),
        ("0001111111-26-000005", "2026-09-01", "SCHEDULE 13D", "", "primary_doc.xml"),
        ("0001111111-26-000003", "2026-08-10", "13F-HR", "2026-06-30", "primary_doc.xml"),
    ])

    def table(rows):
        body = "".join("""<infoTable><nameOfIssuer>%s</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>%s</cusip><value>%d</value><shrsOrPrnAmt><sshPrnamt>%d</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
<investmentDiscretion>SOLE</investmentDiscretion></infoTable>""" % r for r in rows)
        return ('<?xml version="1.0"?><informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">'
                + body + "</informationTable>")

    for acc, rows in (
            (q3, [("ACME ROBOTICS INC", "004000101", 90_000_000, 6_000_000),     # NEW
                  ("BETA FOODS CORP", "074000101", 40_000_000, 2_000_000),       # +100 %
                  ("GAMMA MINING CO", "364000101", 30_000_000, 3_100_000)]),     # +3 %, hidden
            (q2, [("BETA FOODS CORP", "074000101", 20_000_000, 1_000_000),
                  ("GAMMA MINING CO", "364000101", 29_000_000, 3_000_000),
                  ("DELTA SHIPPING INC", "247000101", 15_000_000, 500_000)])):  # SOLD OUT
        folder = "Archives/edgar/data/5555555/%s" % acc.replace("-", "")
        put(arch(folder + "/index.json"), {"directory": {"item": [
            {"name": "primary_doc.xml"}, {"name": "infotable.xml"}]}})
        put(arch(folder + "/infotable.xml"), table(rows))
        put(arch(folder + "/primary_doc.xml"),
            "<edgarSubmission><tableValueTotal>%d</tableValueTotal></edgarSubmission>"
            % sum(r[2] for r in rows))

    put("filers.json", {"filers": [
        {"cik": FUND13F, "name": "Whale Capital LP", "person": "Jane Whale",
         "aliases": ["whale", "jane whale"]},
        {"cik": ACTIVIST, "name": "Activist Partners LP", "person": "Max Pressure",
         "aliases": ["activist", "max pressure"]},
    ]})
    print("wrote %d files to %s" % (len(os.listdir(OUT)), OUT))


if __name__ == "__main__":
    main()
