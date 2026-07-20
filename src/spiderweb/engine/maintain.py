import json
import sqlite3
from typing import Optional
from spiderweb.engine.db import get_db

class MaintainService:
    """
    提供图谱的手动维护能力，遵循“连接即存在”的哲学。
    所有操作均作为非破坏性扩展，不修改或删除现有数据。
    """

    def __init__(self, db_path: str, domain_config: dict):
        self.db = get_db(db_path)
        self.domain_config = domain_config

    def _validate_entity(self, name: str) -> bool:
        """检查实体是否存在。"""
        return self.db.execute("SELECT 1 FROM entities WHERE canonical_name = ?", (name,)).fetchone() is not None

    def register(self, name: str, etype: str, book: Optional[str] = None, target_entity: Optional[str] = None, relation_type: str = "MENTIONS") -> dict:
        """
        注册新实体并强制建立拓扑连接。

        参数:
            name (str): 实体名称。
            etype (str): 实体类型 (person, location, event, concept, work, scene, element)。
            book (Optional[str]): 关联的书名，若提供，自动建立 LOCATED_IN 边。
            target_entity (Optional[str]): 关联的其他实体名称，若提供，自动建立指定关系边。
            relation_type (str): 当 target_entity 存在时，使用的关系类型，默认为 'MENTIONS'。

        返回:
            dict: 包含 'ok' 布尔值和 'message' 描述结果。
        """
        if self._validate_entity(name):
            return {"ok": False, "message": f"实体 '{name}' 已存在。"}

        # 强制闭环校验：拒绝产生孤立节点
        if not book and not target_entity:
            return {"ok": False, "message": "注册失败：禁止注册孤立节点。请提供 book 关联或 target_entity 关联。"}

        with self.db:
            self.db.execute(
                "INSERT INTO entities (canonical_name, entity_type, source) VALUES (?, ?, ?)",
                (name, etype, "manual_refinement")
            )

            if book:
                self.connect(name, book, "LOCATED_IN")
            if target_entity:
                self.connect(name, target_entity, relation_type)

        return {"ok": True, "message": f"实体 '{name}' ({etype}) 已成功入网。"}

    def connect(self, entity_a: str, entity_b: str, relation_type: str, weight: float = 3.0) -> dict:
        """
        在两个已存在的实体之间建立指定类型的关系。

        参数:
            entity_a (str): 关系发起方实体名。
            entity_b (str): 关系接收方实体名。
            relation_type (str): 关系类型 (如: KNOWS, IS_WIFE_OF, MENTIONS 等)。
            weight (float): 关系的权重，默认为 3.0。

        返回:
            dict: 操作结果状态。
        """
        if not self._validate_entity(entity_a) or not self._validate_entity(entity_b):
             return {"ok": False, "message": "实体不存在，请先注册实体。"}

        # 幂等性插入：若已存在同类边则跳过
        try:
            self.db.execute(
                "INSERT INTO relations (entity_a, entity_b, relation_type, weight) VALUES (?, ?, ?, ?)",
                (entity_a, entity_b, relation_type, weight)
            )
            return {"ok": True, "message": f"已成功建立 {relation_type} 关系。"}
        except sqlite3.IntegrityError:
            return {"ok": True, "message": "关系已存在，无需重复建立。"}

    def annotate(self, entity_a: str, entity_b: str, note: str, relation_type: Optional[str] = None) -> dict:
        """
        给实体间的关系边添加语义备注。备注存储在 relations 表的 metadata 字段中。

        参数:
            entity_a (str): 实体A。
            entity_b (str): 实体B。
            note (str): 需要添加的备注内容。
            relation_type (Optional[str]): 边类型，若不指定，则遍历两个实体间的所有边并添加。

        返回:
            dict: 操作结果状态。
        """
        query = "SELECT id, metadata FROM relations WHERE entity_a = ? AND entity_b = ?"
        params = [entity_a, entity_b]
        if relation_type:
            query += " AND relation_type = ?"
            params.append(relation_type)

        edges = self.db.execute(query, params).fetchall()
        if not edges:
            return {"ok": False, "message": "未找到相关关系边。"}

        for edge_id, metadata in edges:
            meta_dict = json.loads(metadata or "{}")
            notes = meta_dict.get("notes", [])
            if note not in notes:
                notes.append(note)
                meta_dict["notes"] = notes
                self.db.execute(
                    "UPDATE relations SET metadata = ? WHERE id = ?",
                    (json.dumps(meta_dict), edge_id)
                )

        return {"ok": True, "message": f"已成功添加备注到 {len(edges)} 条边。"}
