import os
import csv
import time
from typing import List, Dict, Any, Optional

try:
    from fpdf import FPDF
    HAS_FPDF = True
except ImportError:
    HAS_FPDF = False


class ReportManager:
    def __init__(self, reports_dir: str = "reports"):
        self.reports_dir = reports_dir
        os.makedirs(self.reports_dir, exist_ok=True)
        self.is_recording: bool = False
        self.meta: Dict[str, Any] = {}
        self.records: List[Dict[str, Any]] = []
        self.latest_csv: Optional[str] = None
        self.latest_pdf: Optional[str] = None

    def start_session(self, mode: str, params: Dict[str, Any]):
        if self.is_recording:
            # Не теряем данные незавершённой сессии: сначала записываем её отчёт.
            self.finish_session()
        self.is_recording = True
        self.records = []
        self.meta = {
            "test_id": time.strftime("%Y%m%d_%H%M%S"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "params": params,
            "summary": {}
        }

    def add_point(self, telemetry: Dict[str, Any], autobench_state: Optional[Dict[str, Any]] = None):
        if not self.is_recording:
            return
        g = telemetry.get("global", {})
        c = telemetry.get("l7_client", {})
        ab = autobench_state or {}
        self.records.append({
            "timestamp": telemetry.get("timestamp", time.strftime("%H:%M:%S")),
            "tx_gbps": g.get("tx_gbps", 0.0),
            "rx_gbps": g.get("rx_gbps", 0.0),
            "tx_mpps": g.get("tx_mpps", 0.0),
            "rx_mpps": g.get("rx_mpps", 0.0),
            "active_flows": c.get("active_flows", 0),
            "est_flows": c.get("est_flows", 0),
            "tx_bw_l7_mbps": c.get("tx_bw_l7_mbps", 0.0),
            "rx_bw_l7_mbps": c.get("rx_bw_l7_mbps", 0.0),
            "conn_drops": c.get("conn_drops", 0),
            "syn_timeouts": c.get("syn_timeouts", 0),
            "ab_phase": ab.get("phase", "-"),
            "ab_gbps": ab.get("current_gbps", "-")
        })

    def finish_session(self, summary: Optional[Dict[str, Any]] = None):
        if not self.is_recording:
            return
        self.is_recording = False
        self.meta["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.meta["summary"] = self._compute_summary(summary or {})

        test_id = self.meta["test_id"]
        csv_path = os.path.join(self.reports_dir, f"netstrike_report_{test_id}.csv")
        pdf_path = os.path.join(self.reports_dir, f"netstrike_report_{test_id}.pdf")

        self._write_csv(csv_path)
        self._write_pdf(pdf_path)

        self.latest_csv = csv_path
        self.latest_pdf = pdf_path

    def _compute_summary(self, extra_summary: Dict[str, Any]) -> Dict[str, Any]:
        if not self.records:
            return extra_summary
        max_tx = max(r["tx_gbps"] for r in self.records)
        max_rx = max(r["rx_gbps"] for r in self.records)
        max_pps = max(r["tx_mpps"] for r in self.records)
        max_flows = max(r["active_flows"] for r in self.records)
        total_drops = self.records[-1]["conn_drops"]
        total_syn_to = self.records[-1]["syn_timeouts"]

        base = {
            "samples_count": len(self.records),
            "peak_tx_gbps": round(max_tx, 3),
            "peak_rx_gbps": round(max_rx, 3),
            "peak_tx_mpps": round(max_pps, 3),
            "peak_active_flows": max_flows,
            "final_conn_drops": total_drops,
            "final_syn_timeouts": total_syn_to
        }
        base.update(extra_summary)
        return base

    def _write_csv(self, path: str):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["# NetStrike Benchmark Report"])
            writer.writerow(["Test ID", self.meta.get("test_id")])
            writer.writerow(["Started At", self.meta.get("started_at")])
            writer.writerow(["Finished At", self.meta.get("finished_at")])
            writer.writerow(["Mode", self.meta.get("mode")])
            for k, v in self.meta.get("summary", {}).items():
                writer.writerow([f"Summary: {k}", v])
            writer.writerow([])

            headers = [
                "timestamp", "tx_gbps", "rx_gbps", "tx_mpps", "rx_mpps",
                "active_flows", "est_flows", "tx_bw_l7_mbps", "rx_bw_l7_mbps",
                "conn_drops", "syn_timeouts", "ab_phase", "ab_gbps"
            ]
            writer.writerow(headers)
            for r in self.records:
                writer.writerow([r[h] for h in headers])

    def _transliterate(self, text: str) -> str:
        mapping = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
            'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
            'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
            'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
            'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Shch',
            'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya'
        }
        return "".join(mapping.get(ch, ch) for ch in text)

    def _find_unicode_font(self) -> Optional[str]:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "C:\\Windows\\Fonts\\arial.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _safe_text(self, text: Any, unicode_ready: bool) -> str:
        s = str(text)
        if unicode_ready:
            return s
        s = self._transliterate(s)
        return s.encode("latin-1", errors="replace").decode("latin-1")

    def _write_pdf(self, path: str):
        if HAS_FPDF:
            self._write_pdf_fpdf(path)
        else:
            self._write_pdf_fallback(path)

    def _write_pdf_fpdf(self, path: str):
        pdf = FPDF()
        pdf.add_page()

        font_path = self._find_unicode_font()
        unicode_ready = False
        main_font = "Helvetica"

        if font_path:
            try:
                pdf.add_font("DejaVu", "", font_path, uni=True)
                pdf.add_font("DejaVu", "B", font_path, uni=True)
                main_font = "DejaVu"
                unicode_ready = True
            except Exception:
                unicode_ready = False

        pdf.set_font(main_font, "B", 16)
        pdf.cell(0, 10, f"NetStrike Benchmark Report #{self.meta.get('test_id')}", ln=True)

        pdf.set_font(main_font, "", 10)
        pdf.cell(0, 6, f"Started: {self.meta.get('started_at')} | Finished: {self.meta.get('finished_at')}", ln=True)
        pdf.cell(0, 6, f"Profile Mode: {self.meta.get('mode')}", ln=True)
        pdf.ln(4)

        pdf.set_font(main_font, "B", 12)
        pdf.cell(0, 8, "Summary Metrics", ln=True)
        pdf.set_font(main_font, "", 10)
        for k, v in self.meta.get("summary", {}).items():
            line = self._safe_text(f"- {k}: {v}", unicode_ready)
            pdf.cell(0, 6, line, ln=True)
        pdf.ln(4)

        pdf.set_font(main_font, "B", 11)
        pdf.cell(0, 8, "Telemetry Timeline (Last 40 samples)", ln=True)
        pdf.set_font("Courier", "B", 8)
        header_line = f"{'Time':<10} {'TX(G)':>7} {'RX(G)':>7} {'PPS(M)':>7} {'Flows':>8} {'Drops':>7} {'Phase':<10} {'Gbps':>6}"
        pdf.cell(0, 5, header_line, ln=True)
        pdf.set_font("Courier", "", 8)
        for r in self.records[-40:]:
            line = (
                f"{str(r['timestamp']):<10} {r['tx_gbps']:>7.2f} {r['rx_gbps']:>7.2f} "
                f"{r['tx_mpps']:>7.2f} {r['active_flows']:>8} {r['conn_drops']:>7} "
                f"{str(r['ab_phase']):<10} {str(r['ab_gbps']):>6}"
            )
            pdf.cell(0, 5, self._safe_text(line, False), ln=True)

        pdf.output(path)

    def _write_pdf_fallback(self, path: str):
        lines = [
            f"NetStrike Benchmark Report #{self.meta.get('test_id')}",
            f"Started: {self.meta.get('started_at')} | Finished: {self.meta.get('finished_at')}",
            f"Mode: {self.meta.get('mode')}",
            "--- SUMMARY ---"
        ]
        for k, v in self.meta.get("summary", {}).items():
            lines.append(f"{k}: {v}")
        lines.append("--- TIMELINE (LAST 30 SAMPLES) ---")
        lines.append("Time       TX_Gbps RX_Gbps TX_Mpps Flows   Drops   Phase      Gbps")
        for r in self.records[-30:]:
            lines.append(
                f"{str(r['timestamp']):<10} {r['tx_gbps']:<7.2f} {r['rx_gbps']:<7.2f} "
                f"{r['tx_mpps']:<7.2f} {r['active_flows']:<7} {r['conn_drops']:<7} "
                f"{str(r['ab_phase']):<10} {r['ab_gbps']}"
            )

        content_ops = ["BT", "/F1 9 Tf", "40 800 Td", "12 TL"]
        for line in lines:
            clean_line = self._transliterate(line)
            safe = clean_line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content_ops.append(f"({safe}) '")
        content_ops.append("ET")
        stream = "\n".join(content_ops).encode("latin-1", errors="replace")

        objs = [
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >> endobj\n",
            b"4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Courier >> endobj\n",
            f"5 0 obj << /Length {len(stream)} >> stream\n".encode("ascii") + stream + b"\nendstream endobj\n"
        ]

        with open(path, "wb") as f:
            f.write(b"%PDF-1.4\n")
            offsets = []
            for obj in objs:
                offsets.append(f.tell())
                f.write(obj)
            xref_pos = f.tell()
            f.write(f"xref\n0 {len(objs) + 1}\n".encode("ascii"))
            f.write(b"0000000000 65535 f \n")
            for off in offsets:
                f.write(f"{off:010d} 00000 n \n".encode("ascii"))
            f.write(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
