"""Generate the new synthetic development taskbook."""

import json
from hashlib import sha256
from pathlib import Path

SCENARIOS = """Inventory reconciliation|cold-bin tally|cycle counter|manifest signoff|bin scan
Metering dispute|hourly import|billing meter|credit release|meter interval
Warehouse handoff|parcel seal|sort scan|shipment release|sorter entry
Render audit|frame manifest|frame counter|archive export|render queue
Library recovery|volume accession|shelf scan|catalog publication|shelf ticket
Transit reporting|route change|route register|notice publication|journey entry
Training survey|consent revision|survey collector|survey export|collector batch
Lake sampling|sample chain|vial weight|report release|lab handoff
Fixture calibration|gauge serial|reference reading|line restart|fixture sweep
Translation delivery|locale revision|term check|translation release|catalog check
Museum loan|case seal|registrar receipt|courier pickup|intake log
Parcel claims|dispatch weight|carrier weigh-in|claims decision|scale reading
Battery inventory|pack lot|bench measurement|lot transfer|station scan
Food ledger|batch expiry|warehouse count|donation release|inventory scan
Imaging handoff|image series|checksum audit|archive transfer|image archive
Forecast archive|regional revision|model receipt|bulletin release|forecast trace
Seating assignment|venue map|reservation lookup|ticket release|layout inspection
Audio restoration|session reel|track duration|master publication|tape inspection
Curriculum review|edition ledger|module audit|curriculum release|lesson review
Crop sampling|plot label|field observation|dataset release|plot inspection
Exhibit lights|dimmer map|lighting meter|scene publish|channel observation
Dataset migration|partition map|row counter|catalog promotion|partition audit
Quarterly close|account mapping|balance proof|ledger close|reconciliation log
Track maintenance|section mark|gauge survey|inspection release|survey checkpoint
Dye batch review|color standard|batch spectrum|batch dispatch|lab spectrum
Plant inspection|valve tag|flow meter|work order close|inspection pass
Assay handoff|plate barcode|control reading|result release|plate scan
Map digitization|sheet revision|feature tally|map publication|layer inspection
Budget consolidation|project code|allocation sum|finance signoff|budget worksheet
Delivery audit|dock allocation|arrival proof|invoice release|delivery receipt""".splitlines()


def taskbook():
    for n, scenario in enumerate(SCENARIOS, 1):
        domain, object_name, measurement, action, log = scenario.split("|")
        incident = f"R1-{log.split()[0].upper()}-{n:03}"
        receipt = sha256(f"{scenario} receipt".encode()).hexdigest()[:9].upper()
        failure = measurement.upper().replace(" ", "_") + "_STALE"
        finding = f"MATCHED-{receipt}"
        yield dict(
            case_id=f"e2-{n:02}",
            incident=incident,
            task=f"Reconcile {object_name} with {measurement} for {incident} "
            f"in the {domain.lower()} and prepare the handoff.",
            constraint=f"Use only the countersigned {object_name} at the pinned revision.",
            pending_approval=f"Wait for owner approval before {action}.",
            open_question=f"Will the next {measurement} reproduce the signed result?",
            failure_code=failure,
            finding_code=finding,
            evidence_id=f"r1-{n:02}-signed",
            diagnostic=f"{domain}: two operators confirmed {measurement} against {object_name}.\n"
            f"FINAL_CODE={finding}\n",
            sequence=[
                f"{log}: sampled {object_name} manifest before {measurement} reconciliation.",
                f"Operator reconciled {measurement} entries with {object_name} revision table.",
                f"Waited for a countersigned {object_name} from the second shift.",
                f"Review queue still awaiting {action}.",
            ],
        )


if __name__ == "__main__":
    Path(__file__).with_name("development.jsonl").write_text(
        "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in taskbook())
    )
