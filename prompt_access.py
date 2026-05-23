from marketplace.config import marketplace_discovery_enabled
from marketplace.services.entitlements import (
    active_entitlement_condition,
    user_has_pack_access as user_has_pack_entitlement_access,
)
from models import User


async def get_user_accessible_prompts(user: User, cursor, all_prompts_access: bool = False, public_prompts_access: bool = False, category_access: str = None):
    """
    Get prompts accessible to a user.

    Args:
        category_access: JSON string of category IDs or None.
            - None = access to all public prompt categories
            - '[]' = no access to public prompts
            - '[1,2,5]' = access only to prompts in those categories
    """
    if await user.is_admin or all_prompts_access:
        await cursor.execute('''
            SELECT p.id, p.name, u.username as created_by_username, p.public_id,
                   CASE WHEN pcd.is_active = 1 AND pcd.verification_status = 1
                        THEN pcd.custom_domain ELSE NULL END as custom_domain,
                   CASE WHEN fp.prompt_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite,
                   CASE WHEN opp.permission_level = 'owner' THEN 1
                        WHEN p.created_by_user_id = ?
                             AND NOT EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS
                                             WHERE prompt_id = p.id AND permission_level = 'owner')
                        THEN 1 ELSE 0 END as is_mine
            FROM PROMPTS p
            JOIN USERS u ON p.created_by_user_id = u.id
            LEFT JOIN PROMPT_CUSTOM_DOMAINS pcd ON p.id = pcd.prompt_id
            LEFT JOIN FAVORITE_PROMPTS fp ON fp.prompt_id = p.id AND fp.user_id = ?
            LEFT JOIN PROMPT_PERMISSIONS opp ON opp.prompt_id = p.id AND opp.user_id = ? AND opp.permission_level = 'owner'
            ORDER BY p.name COLLATE NOCASE
        ''', (user.id, user.id, user.id))
    elif await user.is_user:
        query = f'''
            SELECT DISTINCT p.id, p.name, u.username as created_by_username, p.public_id,
                   CASE WHEN pcd.is_active = 1 AND pcd.verification_status = 1
                        THEN pcd.custom_domain ELSE NULL END as custom_domain,
                   CASE WHEN fp.prompt_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite,
                   CASE WHEN opp.permission_level = 'owner' THEN 1
                        WHEN p.created_by_user_id = ?
                             AND NOT EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS
                                             WHERE prompt_id = p.id AND permission_level = 'owner')
                        THEN 1 ELSE 0 END as is_mine
            FROM PROMPTS p
            JOIN USERS u ON p.created_by_user_id = u.id
            LEFT JOIN PROMPT_PERMISSIONS pp ON p.id = pp.prompt_id AND pp.user_id = ?
            LEFT JOIN PROMPT_CUSTOM_DOMAINS pcd ON p.id = pcd.prompt_id
            LEFT JOIN FAVORITE_PROMPTS fp ON fp.prompt_id = p.id AND fp.user_id = ?
            LEFT JOIN PROMPT_PERMISSIONS opp ON opp.prompt_id = p.id AND opp.user_id = ? AND opp.permission_level = 'owner'
            WHERE p.created_by_user_id = ?
                OR (pp.permission_level IN ('edit', 'owner'))
                OR EXISTS (
                    SELECT 1 FROM ENTITLEMENTS e_prompt
                    WHERE e_prompt.user_id = ?
                      AND e_prompt.asset_type = 'prompt'
                      AND e_prompt.asset_id = p.id
                      AND {active_entitlement_condition("e_prompt")}
                )
                OR p.id IN (
                    SELECT pi.prompt_id FROM PACK_ITEMS pi
                    JOIN ENTITLEMENTS e_pack ON e_pack.asset_type = 'pack'
                        AND e_pack.asset_id = pi.pack_id
                    WHERE e_pack.user_id = ?
                      AND pi.is_active = 1
                      AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
                      AND {active_entitlement_condition("e_pack")}
                )
        '''
        params = [user.id, user.id, user.id, user.id, user.id, user.id, user.id]

        if public_prompts_access and marketplace_discovery_enabled():
            if category_access is None:
                query += " OR (p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0))"
            else:
                query += """ OR (p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0) AND EXISTS (
                    SELECT 1 FROM PROMPT_CATEGORIES pc
                    WHERE pc.prompt_id = p.id
                    AND pc.category_id IN (SELECT value FROM json_each(?))
                ))"""
                params.append(category_access)

        query += " ORDER BY p.name COLLATE NOCASE"
        await cursor.execute(query, params)
    else:
        query = f'''
            SELECT DISTINCT p.id, p.name, u.username as created_by_username, p.public_id,
                   CASE WHEN pcd.is_active = 1 AND pcd.verification_status = 1
                        THEN pcd.custom_domain ELSE NULL END as custom_domain,
                   CASE WHEN fp.prompt_id IS NOT NULL THEN 1 ELSE 0 END as is_favorite,
                   CASE WHEN opp.permission_level = 'owner' THEN 1
                        WHEN p.created_by_user_id = ?
                             AND NOT EXISTS (SELECT 1 FROM PROMPT_PERMISSIONS
                                             WHERE prompt_id = p.id AND permission_level = 'owner')
                        THEN 1 ELSE 0 END as is_mine
            FROM PROMPTS p
            JOIN USERS u ON p.created_by_user_id = u.id
            LEFT JOIN PROMPT_PERMISSIONS pp ON p.id = pp.prompt_id AND pp.user_id = ?
            LEFT JOIN PROMPT_CUSTOM_DOMAINS pcd ON p.id = pcd.prompt_id
            LEFT JOIN FAVORITE_PROMPTS fp ON fp.prompt_id = p.id AND fp.user_id = ?
            LEFT JOIN PROMPT_PERMISSIONS opp ON opp.prompt_id = p.id AND opp.user_id = ? AND opp.permission_level = 'owner'
            WHERE p.created_by_user_id = ?
                OR (pp.permission_level IN ('edit', 'owner'))
                OR EXISTS (
                    SELECT 1 FROM ENTITLEMENTS e_prompt
                    WHERE e_prompt.user_id = ?
                      AND e_prompt.asset_type = 'prompt'
                      AND e_prompt.asset_id = p.id
                      AND {active_entitlement_condition("e_prompt")}
                )
                OR p.id IN (
                    SELECT pi.prompt_id FROM PACK_ITEMS pi
                    JOIN ENTITLEMENTS e_pack ON e_pack.asset_type = 'pack'
                        AND e_pack.asset_id = pi.pack_id
                    WHERE e_pack.user_id = ?
                      AND pi.is_active = 1
                      AND (pi.disable_at IS NULL OR pi.disable_at > datetime('now'))
                      AND {active_entitlement_condition("e_pack")}
                )
        '''
        params = [user.id, user.id, user.id, user.id, user.id, user.id, user.id]

        if public_prompts_access and marketplace_discovery_enabled():
            if category_access is None:
                query += " OR (p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0))"
            else:
                query += """ OR (p.public = 1 AND (p.purchase_price IS NULL OR p.purchase_price <= 0) AND EXISTS (
                    SELECT 1 FROM PROMPT_CATEGORIES pc
                    WHERE pc.prompt_id = p.id
                    AND pc.category_id IN (SELECT value FROM json_each(?))
                ))"""
                params.append(category_access)

        query += " ORDER BY p.name COLLATE NOCASE"

        await cursor.execute(query, params)

    prompts = await cursor.fetchall()
    return [{"id": p[0], "text": p[1], "created_by_username": p[2], "public_id": p[3], "name": p[1], "custom_domain": p[4], "is_favorite": bool(p[5]), "is_mine": bool(p[6])} for p in prompts]


async def can_user_access_pack(user: User, pack_id: int, cursor) -> bool:
    """Check if user can access a pack. Returns True for admins, pack owners, or active entitlements."""
    if await user.is_admin:
        return True
    return await user_has_pack_entitlement_access(cursor, user_id=user.id, pack_id=pack_id)
