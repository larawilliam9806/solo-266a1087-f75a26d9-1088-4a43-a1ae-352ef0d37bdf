"""Flask app for box-by-box archive catalogue reconciliation."""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from .canonical import (
    CsvError,
    clean_text,
    group_items,
    parse_items_csv,
    parse_manifest_csv,
)
from .store import (
    BatchLockedError,
    ManifestMismatchError,
    NotFoundError,
    StaleBoxError,
    Store,
)
from .views import build_board

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "instance" / "reconcile.sqlite3"

PROBLEM_MESSAGES = {
    "missing_manifest": "该盒不在已导入的旧库清单中。",
    "count_mismatch": "回传明细的条数与旧库清单不符，已拒绝保存。",
    "digest_mismatch": "回传明细的 SHA-256 摘要与旧库清单不符，已拒绝保存。",
}


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-archive-reconcile"),
        MAX_CONTENT_LENGTH=32 * 1024 * 1024,
        DATABASE=os.environ.get("ARCHIVE_DB", str(DEFAULT_DB)),
    )
    if test_config:
        app.config.update(test_config)

    store = Store(app.config["DATABASE"])
    app.extensions["store"] = store

    def get_store() -> Store:
        return app.extensions["store"]

    def load_batch(batch_id: int):
        batch = get_store().get_batch(batch_id)
        if batch is None:
            abort(404)
        return batch

    @app.template_filter("dt")
    def format_dt(value):
        if not value:
            return "—"
        from datetime import datetime

        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")

    @app.template_filter("shortdigest")
    def short_digest(value):
        if not value:
            return "—"
        return f"{value[:12]}…{value[-6:]}"

    # ------------------------------------------------------------------
    # batch list / creation
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            batches=get_store().list_batches(),
        )

    @app.get("/healthz")
    def healthz():
        get_store().ping()
        return {"status": "ok"}

    @app.post("/batches")
    def create_batch():
        label = clean_text(request.form.get("label"))
        manifest_file = request.files.get("manifest")
        items_file = request.files.get("items")
        form_errors = []
        manifest = None
        items = None

        if not label:
            label = "迁库核对"
        if manifest_file is None or not manifest_file.filename:
            form_errors.append("请选择旧库清单 CSV。")
        if items_file is None or not items_file.filename:
            form_errors.append("请选择新库题录 CSV。")
        if not form_errors:
            try:
                manifest = parse_manifest_csv(manifest_file.read())
                if not manifest:
                    form_errors.append("旧库清单为空，至少需要一个盒。")
            except CsvError as exc:
                form_errors.append(f"旧库清单：{exc}")
            try:
                items = parse_items_csv(items_file.read())
            except CsvError as exc:
                form_errors.append(f"新库题录：{exc}")

        if form_errors:
            for message in form_errors:
                flash(message, "error")
            return render_template(
                "index.html",
                batches=get_store().list_batches(),
                label=label,
            ), 400

        batch = get_store().create_batch(label, manifest.values())
        grouped = group_items(items)
        try:
            get_store().import_snapshot(batch.id, grouped, source="initial")
        except Exception:
            get_store().delete_batch(batch.id)
            raise
        empty_boxes = sum(1 for box_no in manifest if box_no not in grouped)
        flash(
            f"已建立批次 #{batch.id}：旧库清单 {len(manifest)} 盒，"
            f"新库题录 {len(grouped)} 盒（其中 {empty_boxes} 个清单盒按空盒比较）。",
            "success",
        )
        return redirect(url_for("board", batch_id=batch.id))

    # ------------------------------------------------------------------
    # board
    # ------------------------------------------------------------------

    @app.get("/batches/<int:batch_id>")
    def board(batch_id: int):
        batch = load_batch(batch_id)
        board_view = build_board(get_store(), batch_id)
        return render_template("board.html", batch=batch, board=board_view)

    @app.get("/batches/<int:batch_id>/report.csv")
    def report_csv(batch_id: int):
        batch = load_batch(batch_id)
        board_view = build_board(get_store(), batch_id)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            [
                "盒号",
                "状态",
                "清单条数",
                "清单摘要",
                "新库条数",
                "新库摘要",
                "明细条数",
                "明细摘要",
                "漏录",
                "多录",
                "字段变更",
                "已确认",
            ]
        )
        for box in board_view.boxes:
            writer.writerow(
                [
                    box.box_no,
                    box.status_label,
                    "" if box.manifest_count is None else box.manifest_count,
                    box.manifest_digest or "",
                    "" if box.new_count is None else box.new_count,
                    box.new_digest or "",
                    "" if box.detail_count is None else box.detail_count,
                    box.detail_digest or "",
                    box.missing if box.status in ("differences", "match") else "",
                    box.extra if box.status in ("differences", "match") else "",
                    box.changed if box.status in ("differences", "match") else "",
                    "是" if box.finalized else "否",
                ]
            )
        filename = f"reconcile-batch-{batch.id}.csv"
        return Response(
            "﻿" + buffer.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/batches/<int:batch_id>/finalize")
    def finalize_batch(batch_id: int):
        load_batch(batch_id)
        if get_store().finalize_batch(batch_id):
            flash("批次已归档，之后不能再换交题录或修改结论。", "success")
        else:
            flash("批次已是归档状态。", "info")
        return redirect(url_for("board", batch_id=batch_id))

    @app.post("/batches/<int:batch_id>/reopen")
    def reopen_batch(batch_id: int):
        load_batch(batch_id)
        if get_store().reopen_batch(batch_id):
            flash("批次已重新打开，可以继续换交与核对。", "success")
        else:
            flash("批次本就处于打开状态。", "info")
        return redirect(url_for("board", batch_id=batch_id))

    # ------------------------------------------------------------------
    # box page
    # ------------------------------------------------------------------

    def render_box(
        batch,
        box_no: str,
        *,
        error: str | None = None,
        status: int = 200,
        expected_version_id: int | None = None,
    ):
        store = get_store()
        manifest = store.get_manifest(batch.id)
        if box_no not in manifest:
            versions = store.current_versions(batch.id)
            if box_no not in versions:
                abort(404)
        board_view = build_board(store, batch.id)
        box = board_view.get(box_no)
        if box is None:
            abort(404)
        version = store.get_current_version(batch.id, box_no)
        versions = store.version_history(batch.id, box_no)
        current_items = store.load_version_items(version.id, box_no) if version else []
        detail = store.get_detail(batch.id, box_no)
        detail_items = store.load_detail_items(detail.id, box_no) if detail else []
        reconciliation = store.get_reconciliation(batch.id, box_no)
        differences = (
            store.get_differences(reconciliation["id"]) if reconciliation else []
        )
        if expected_version_id is None:
            expected_version_id = box.new_version_id
        return (
            render_template(
                "box.html",
                batch=batch,
                box=box,
                version=version,
                versions=versions,
                current_items=current_items,
                detail=detail,
                detail_items=detail_items,
                reconciliation=reconciliation,
                differences=differences,
                expected_version_id=expected_version_id,
                error=error,
            ),
            status,
        )

    @app.get("/batches/<int:batch_id>/boxes/<path:box_no>")
    def box_detail(batch_id: int, box_no: str):
        batch = load_batch(batch_id)
        return render_box(batch, clean_text(box_no))

    def handle_stale(batch, box_no: str, exc: StaleBoxError):
        message = (
            f"盒 {box_no} 的内容在核对途中已变更（当前版本 "
            f"{exc.current_version if exc.current_version is not None else 0}）。"
            "本次结果已拒绝保存，原有数据与有效结论保持不变；请核对最新内容后重试。"
        )
        return render_box(
            batch,
            box_no,
            error=message,
            status=409,
        )

    def handle_batch_error(batch, box_no: str, exc: Exception):
        if isinstance(exc, BatchLockedError):
            return render_box(batch, box_no, error=str(exc), status=409)
        if isinstance(exc, NotFoundError):
            return render_box(batch, box_no, error=str(exc), status=409)
        if isinstance(exc, CsvError):
            return render_box(batch, box_no, error=str(exc), status=400)
        raise exc

    # ------------------------------------------------------------------
    # replacing a box's new-archive content
    # ------------------------------------------------------------------

    @app.post("/batches/<int:batch_id>/boxes/<path:box_no>/items")
    def replace_box_items(batch_id: int, box_no: str):
        batch = load_batch(batch_id)
        box_no = clean_text(box_no)
        upload = request.files.get("items")
        raw_expected = request.form.get("expected_version_id", "")
        try:
            expected_version_id = int(raw_expected) if raw_expected else 0
        except ValueError:
            expected_version_id = 0
        if upload is None or not upload.filename:
            return render_box(batch, box_no, error="请选择该盒的新题录 CSV。", status=400)
        try:
            items = parse_items_csv(upload.read(), expected_box=box_no)
        except CsvError as exc:
            return render_box(batch, box_no, error=f"换交失败：{exc}", status=400)
        try:
            result = get_store().import_snapshot(
                batch.id,
                {box_no: items},
                source="box-replacement",
                expected_versions={box_no: expected_version_id},
            )
        except StaleBoxError as exc:
            return handle_stale(batch, box_no, exc)
        except (BatchLockedError, NotFoundError) as exc:
            return handle_batch_error(batch, box_no, exc)

        outcome = result[box_no]
        if outcome["created"]:
            flash(
                f"盒 {box_no} 已换交为第 {outcome['version']} 版"
                f"（{outcome['item_count']} 条）。该盒原结论已撤下并按新内容重算。",
                "success",
            )
        else:
            flash(f"盒 {box_no} 内容与当前版本一致，未产生新版本。", "info")
        return redirect(url_for("box_detail", batch_id=batch.id, box_no=box_no))

    @app.post("/batches/<int:batch_id>/boxes/<path:box_no>/detail")
    def submit_detail(batch_id: int, box_no: str):
        batch = load_batch(batch_id)
        box_no = clean_text(box_no)
        upload = request.files.get("detail")
        raw_expected = request.form.get("expected_version_id", "")
        try:
            expected_version_id = int(raw_expected) if raw_expected else 0
        except ValueError:
            expected_version_id = 0
        if upload is None or not upload.filename:
            return render_box(batch, box_no, error="请选择回传的旧库明细 CSV。", status=400)
        try:
            items = parse_items_csv(upload.read(), expected_box=box_no)
        except CsvError as exc:
            return render_box(batch, box_no, error=f"明细校验失败：{exc}", status=400)
        try:
            result = get_store().submit_detail(
                batch.id,
                box_no,
                items,
                expected_version_id=expected_version_id,
            )
        except ManifestMismatchError as exc:
            summary = exc.summary
            detail = ""
            if "manifest_count" in summary:
                detail = (
                    f"（清单 {summary['manifest_count']} 条 / "
                    f"{summary['manifest_digest'][:12]}…，回传 {summary['detail_count']} 条 / "
                    f"{summary['detail_digest'][:12]}…）"
                )
            message = PROBLEM_MESSAGES.get(exc.problem, "明细与清单不符。") + detail
            message += " 原有明细与仍有效的结论保持不变。"
            return render_box(batch, box_no, error=message, status=409)
        except StaleBoxError as exc:
            return handle_stale(batch, box_no, exc)
        except (BatchLockedError, NotFoundError) as exc:
            return handle_batch_error(batch, box_no, exc)

        flash(
            f"盒 {box_no} 明细已通过清单校验（{result['detail_count']} 条，摘要相符）。",
            "success",
        )
        return redirect(url_for("box_detail", batch_id=batch.id, box_no=box_no))

    @app.post("/batches/<int:batch_id>/boxes/<path:box_no>/finalize")
    def finalize_box(batch_id: int, box_no: str):
        batch = load_batch(batch_id)
        box_no = clean_text(box_no)
        raw_expected = request.form.get("expected_version_id", "")
        try:
            expected_version_id = int(raw_expected) if raw_expected else 0
        except ValueError:
            expected_version_id = 0
        try:
            get_store().finalize_box(
                batch.id, box_no, expected_version_id=expected_version_id
            )
        except StaleBoxError as exc:
            return handle_stale(batch, box_no, exc)
        except (BatchLockedError, NotFoundError) as exc:
            return handle_batch_error(batch, box_no, exc)
        flash(f"盒 {box_no} 的核对结论已确认。", "success")
        return redirect(url_for("box_detail", batch_id=batch.id, box_no=box_no))

    @app.get("/batches/<int:batch_id>/boxes/<path:box_no>/differences.csv")
    def differences_csv(batch_id: int, box_no: str):
        load_batch(batch_id)
        store = get_store()
        reconciliation = store.get_reconciliation(batch_id, clean_text(box_no))
        if reconciliation is None:
            abort(404)
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            ["盒号", "档号", "差异类型", "字段", "新值", "旧值"]
        )
        labels = {"missing": "漏录", "extra": "多录", "changed": "字段变更"}
        for difference in store.get_differences(reconciliation["id"]):
            if difference["kind"] == "changed":
                for field in difference["changes"]:
                    if field == "title":
                        writer.writerow(
                            [
                                clean_text(box_no),
                                difference["archive_id"],
                                labels["changed"],
                                "题名",
                                difference["new_title"],
                                difference["old_title"],
                            ]
                        )
                    elif field == "retention":
                        writer.writerow(
                            [
                                clean_text(box_no),
                                difference["archive_id"],
                                labels["changed"],
                                "保管期限",
                                difference["new_retention"],
                                difference["old_retention"],
                            ]
                        )
            else:
                writer.writerow(
                    [
                        clean_text(box_no),
                        difference["archive_id"],
                        labels[difference["kind"]],
                        "整条",
                        ""
                        if difference["new_title"] is None
                        else f"题名={difference['new_title']}；保管期限={difference['new_retention']}",
                        ""
                        if difference["old_title"] is None
                        else f"题名={difference['old_title']}；保管期限={difference['old_retention']}",
                    ]
                )
        return Response(
            "﻿" + buffer.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="box-{clean_text(box_no)}-differences.csv"'
            },
        )

    @app.errorhandler(413)
    def too_large(_error):
        return render_template("error.html", message="上传文件超过 32 MB 限制。"), 413

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", message="未找到该批次或盒。"), 404

    return app


if __name__ == "__main__":  # pragma: no cover
    # `flask --app archive_reconcile.app run` uses the factory; this entry point
    # is for `python -m archive_reconcile.app`.
    create_app().run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )
