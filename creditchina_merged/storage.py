"""TXT/JSON 文件与 MySQL 持久化。"""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DatabaseConfig
from .crawler import EnterpriseRecord


def safe_filename(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name).strip().rstrip(".")
    return cleaned[:180] or "未命名企业"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class FileStorage:
    def __init__(
        self,
        output_dir: Path,
        write_json: bool = True,
        write_text: bool = True,
        write_xlsx: bool = False,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.write_json = write_json
        self.write_text = write_text
        self.write_xlsx = write_xlsx

    def save(self, record: EnterpriseRecord) -> None:
        payload = record.to_dict()
        name = safe_filename(record.name)
        if self.write_json:
            _atomic_write(
                self.output_dir / (name + ".json"),
                json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            )
        if self.write_text:
            lines = []
            for key, item in payload.items():
                if isinstance(item, (dict, list)):
                    rendered = json.dumps(item, ensure_ascii=False, default=str)
                else:
                    rendered = str(item)
                lines.append("%s：%s" % (key, rendered))
            _atomic_write(self.output_dir / (name + ".txt"), "\n".join(lines) + "\n")
        if self.write_xlsx:
            from .exporter import XlsxExporter

            XlsxExporter.export_current_company(
                record,
                self.output_dir / (name + "-本次采集.xlsx"),
            )


class MySQLRepository:
    """保存六类数据，也可读取待采集企业；从不读取代理表。"""

    def __init__(self, config: DatabaseConfig) -> None:
        self.config = config

    def _driver(self) -> Any:
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("MySQL 功能需要先安装 PyMySQL") from exc
        return pymysql

    def connect(self, include_database: bool = True) -> Any:
        pymysql = self._driver()
        kwargs: Dict[str, Any] = {
            "host": self.config.host,
            "port": self.config.port,
            "user": self.config.user,
            "password": self.config.password,
            "charset": self.config.charset,
            "autocommit": False,
        }
        if include_database:
            kwargs["database"] = self.config.database
        return pymysql.connect(**kwargs)

    def init_schema(self, schema_path: Path) -> None:
        connection = self.connect(include_database=False)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET %s"
                    % (
                        self.config.database.replace("`", "``"),
                        self.config.charset.replace("`", ""),
                    )
                )
                cursor.execute("USE `%s`" % self.config.database.replace("`", "``"))
                sql = Path(schema_path).read_text(encoding="utf-8")
                statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
                for statement in statements:
                    cursor.execute(statement)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_pending_companies(self, limit: Optional[int] = None) -> List[str]:
        sql = "SELECT company_name FROM company_test WHERE crawl_flag = 0 ORDER BY company_name"
        if limit is not None:
            sql += " LIMIT %d" % max(0, int(limit))
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                return [str(row[0]) for row in cursor.fetchall() if row and row[0]]
        finally:
            connection.close()

    @staticmethod
    def _raw(item: Dict[str, Any]) -> str:
        return json.dumps(item, ensure_ascii=False, default=str)

    def save(self, record: EnterpriseRecord, mark_task_completed: bool = False) -> None:
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                self._save_base(cursor, record)
                self._save_permissions(cursor, record)
                self._save_penalties(cursor, record)
                self._save_red_list(cursor, record)
                self._save_watch_list(cursor, record)
                self._save_black_list(cursor, record)
                self._save_history(cursor, record)
                if mark_task_completed:
                    cursor.execute(
                        "UPDATE company_test SET crawl_flag = 1 WHERE company_name = %s",
                        (record.name,),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _fingerprint(item: Dict[str, Any]) -> str:
        canonical = json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _record_key(section: str, item: Dict[str, Any]) -> str:
        keys_by_section = {
            "基本信息": ("统一社会信用代码", "工商注册号"),
            "行政许可": ("行政许可决定书文号", "许可机关", "许可决定日期"),
            "行政处罚": ("决定书文号", "处罚名称", "处罚决定日期"),
            "守信红名单": ("序号", "评价年度", "文件名"),
            "重点关注名单": ("注册号", "最新更新日期", "列入决定机关名称"),
            "黑名单": ("案号", "执行依据文号", "立案时间"),
        }
        parts = [str(item.get(key, "")).strip() for key in keys_by_section.get(section, ())]
        return " | ".join(part for part in parts if part)[:512]

    def _save_history(self, cursor: Any, record: EnterpriseRecord) -> None:
        """追加采集批次并对信用内容做只增不减归档。

        历史表从不执行 DELETE。内容完全相同时累计 seen_count，
        内容新增或变更时因指纹不同而追加新行。
        """
        cursor.execute(
            """
            INSERT INTO crawl_history_runs
              (company_name, basic_count, permission_count, penalty_count,
               red_list_count, watch_list_count, black_list_count, errors_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.name,
                1 if record.basic else 0,
                len(record.permissions),
                len(record.penalties),
                len(record.red_list),
                len(record.watch_list),
                len(record.black_list),
                self._raw(record.errors),
            ),
        )

        sections = (
            ("基本信息", [dict(record.basic)] if record.basic else []),
            ("行政许可", record.permissions),
            ("行政处罚", record.penalties),
            ("守信红名单", record.red_list),
            ("重点关注名单", record.watch_list),
            ("黑名单", record.black_list),
        )
        values = []
        for section, items in sections:
            for item in items:
                payload = dict(item)
                values.append(
                    (
                        record.name,
                        section,
                        self._record_key(section, payload),
                        self._fingerprint(payload),
                        self._raw(payload),
                    )
                )
        if values:
            cursor.executemany(
                """
                INSERT INTO company_history
                  (company_name, section_name, record_key, fingerprint, raw_json)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  record_key=VALUES(record_key),
                  last_seen_at=CURRENT_TIMESTAMP,
                  seen_count=seen_count + 1
                """,
                values,
            )

    def load_history(
        self,
        section: Optional[str] = None,
        company_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conditions = []
        params: List[Any] = []
        if section:
            conditions.append("section_name = %s")
            params.append(section)
        if company_name:
            conditions.append("company_name = %s")
            params.append(company_name)
        sql = """
            SELECT company_name, section_name, record_key, raw_json,
                   first_seen_at, last_seen_at, seen_count
            FROM company_history
        """
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY section_name, company_name, first_seen_at, id"

        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, tuple(params) if params else None)
                columns = [description[0] for description in cursor.description]
                rows = cursor.fetchall()
                return [
                    dict(row) if isinstance(row, dict) else dict(zip(columns, row))
                    for row in rows
                ]
        finally:
            connection.close()

    def export_history_penalties(
        self,
        destination: Path,
        company_name: Optional[str] = None,
    ) -> Path:
        from .exporter import XlsxExporter

        return XlsxExporter.export_history_penalties(
            self.load_history("行政处罚", company_name),
            destination,
            company_name=company_name,
        )

    def export_history_all(
        self,
        destination: Path,
        company_name: Optional[str] = None,
    ) -> Path:
        from .exporter import XlsxExporter

        return XlsxExporter.export_history_all(
            self.load_history(company_name=company_name),
            destination,
            company_name=company_name,
        )

    def _save_base(self, cursor: Any, record: EnterpriseRecord) -> None:
        basic = record.basic
        cursor.execute(
            """
            INSERT INTO company_baseinfo
              (company_name, encry_str, company_person, company_status, company_reg,
               company_code, company_type, company_register, company_address,
               company_create, sys_update_time, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
              encry_str=VALUES(encry_str), company_person=VALUES(company_person),
              company_status=VALUES(company_status), company_reg=VALUES(company_reg),
              company_code=VALUES(company_code), company_type=VALUES(company_type),
              company_register=VALUES(company_register), company_address=VALUES(company_address),
              company_create=VALUES(company_create), sys_update_time=VALUES(sys_update_time),
              raw_json=VALUES(raw_json), updated_at=CURRENT_TIMESTAMP
            """,
            (
                record.name,
                record.encry_str,
                basic.get("法人", ""),
                basic.get("企业状态", ""),
                basic.get("工商注册号", ""),
                basic.get("统一社会信用代码", ""),
                basic.get("企业类型", ""),
                basic.get("登记机关", ""),
                basic.get("地址", ""),
                basic.get("成立日期", ""),
                basic.get("系统数据更新时间", ""),
                self._raw(basic),
            ),
        )

    def _save_permissions(self, cursor: Any, record: EnterpriseRecord) -> None:
        sql = """
            INSERT INTO company_xuke_info
              (company_name, company_person, xk_wsh, xk_nr, xk_type, xk_start,
               xk_end, xk_qx, xk_jg, area_code, xk_dfbm, new_uptime, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE raw_json=VALUES(raw_json), updated_at=CURRENT_TIMESTAMP
        """
        values = [
            (
                record.name,
                item.get("许可法人", ""),
                item.get("行政许可决定书文号", ""),
                item.get("内容许可", ""),
                item.get("审核类型", ""),
                item.get("许可决定日期", ""),
                item.get("许可截止日期", ""),
                item.get("许可有效期", ""),
                item.get("许可机关", ""),
                item.get("省份编码", ""),
                item.get("地方编码", ""),
                item.get("数据更新时间", ""),
                self._raw(item),
            )
            for item in record.permissions
        ]
        if values:
            cursor.executemany(sql, values)

    def _save_penalties(self, cursor: Any, record: EnterpriseRecord) -> None:
        sql = """
            INSERT INTO company_chufa_info
              (company_name, company_code, company_person, cf_wsh, cf_name, cf_type,
               cf_jg, cf_sy, cf_yj, cf_zxjg, cf_rq, cf_qx, new_uptime, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE raw_json=VALUES(raw_json), updated_at=CURRENT_TIMESTAMP
        """
        values = [
            (
                record.name,
                item.get("统一社会信用代码", ""),
                item.get("法人代表", ""),
                item.get("决定书文号", ""),
                item.get("处罚名称", ""),
                item.get("处罚类别", ""),
                item.get("处罚结果", ""),
                item.get("处罚事由", ""),
                item.get("处罚依据", ""),
                item.get("处罚机关", ""),
                item.get("处罚决定日期", ""),
                item.get("处罚期限", ""),
                item.get("数据更新时间", ""),
                self._raw(item),
            )
            for item in record.penalties
        ]
        if values:
            cursor.executemany(sql, values)

    def _save_red_list(self, cursor: Any, record: EnterpriseRecord) -> None:
        sql = """
            INSERT INTO company_redList
              (company_name, data_source, data_num, data_type, year_assess,
               file_name, new_update_time, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE raw_json=VALUES(raw_json), updated_at=CURRENT_TIMESTAMP
        """
        values = [
            (
                record.name,
                item.get("数据来源", ""),
                item.get("序号", ""),
                item.get("数据类别", ""),
                item.get("评价年度", ""),
                item.get("文件名", ""),
                item.get("最新更新日期", ""),
                self._raw(item),
            )
            for item in record.red_list
        ]
        if values:
            cursor.executemany(sql, values)

    def _save_watch_list(self, cursor: Any, record: EnterpriseRecord) -> None:
        sql = """
            INSERT INTO company_zdgz_info
              (company_name, company_reg, company_person, data_type, data_source,
               reason_type, set_date, office_name, new_update, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE raw_json=VALUES(raw_json), updated_at=CURRENT_TIMESTAMP
        """
        values = [
            (
                record.name,
                item.get("注册号", ""),
                item.get("法定代表人", ""),
                item.get("数据类别", ""),
                item.get("数据来源", ""),
                item.get("列入经营异常名录原因类型名称", ""),
                item.get("设立日期", ""),
                item.get("列入决定机关名称", ""),
                item.get("最新更新日期", ""),
                self._raw(item),
            )
            for item in record.watch_list
        ]
        if values:
            cursor.executemany(sql, values)

    def _save_black_list(self, cursor: Any, record: EnterpriseRecord) -> None:
        sql = """
            INSERT INTO company_blackList
              (data_type, data_source, case_number, company_name, company_person,
               exe_court, exe_area, exe_file, exe_unit, exe_value, exe_state,
               situation, pubdate, register_time, perform, no_perform, new_update, raw_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE raw_json=VALUES(raw_json), updated_at=CURRENT_TIMESTAMP
        """
        values = [
            (
                item.get("数据类别", ""),
                item.get("数据来源", ""),
                item.get("案号", ""),
                record.name,
                item.get("企业法人姓名", ""),
                item.get("执行法院", ""),
                item.get("地域名称", ""),
                item.get("执行依据文号", ""),
                item.get("作出执行依据单位", ""),
                item.get("法律生效文书确定的义务", ""),
                item.get("被执行人的履行情况", ""),
                item.get("失信被执行人具体情形", ""),
                item.get("发布时间", ""),
                item.get("立案时间", ""),
                item.get("已履行部分", ""),
                item.get("未履行部分", ""),
                item.get("最新更新日期", ""),
                self._raw(item),
            )
            for item in record.black_list
        ]
        if values:
            cursor.executemany(sql, values)
