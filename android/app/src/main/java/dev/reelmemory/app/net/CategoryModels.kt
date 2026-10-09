package dev.reelmemory.app.net

import dev.reelmemory.app.json.JsonObject

/** One node in the backend's per-user hierarchical category tree. */
data class CategoryNode(
    val id: String,
    val name: String,
    val slug: String,
    val path: String,
    val depth: Int,
    val directCount: Int,
    val totalCount: Int,
    val children: List<CategoryNode>,
)

data class CategoryTree(
    val categories: List<CategoryNode>,
    val totalMemories: Int,
)

private fun parseCategoryNode(obj: JsonObject): CategoryNode {
    val children = mutableListOf<CategoryNode>()
    obj.optArray("children")?.let { arr ->
        for (i in 0 until arr.length()) {
            arr.optObject(i)?.let { children += parseCategoryNode(it) }
        }
    }
    return CategoryNode(
        id = obj.optString("id", ""),
        name = obj.optString("name", "Category"),
        slug = obj.optString("slug", ""),
        path = obj.optString("path", ""),
        depth = obj.optInt("depth", 0),
        directCount = obj.optInt("direct_count", 0),
        totalCount = obj.optInt("total_count", 0),
        children = children,
    )
}

/** Parses GET /v1/categories/tree. Never throws; null means malformed JSON. */
fun parseCategoryTree(payload: String): CategoryTree? {
    val obj = try {
        JsonObject(payload)
    } catch (_: Exception) {
        return null
    }
    val roots = mutableListOf<CategoryNode>()
    val arr = obj.optArray("categories") ?: return null
    for (i in 0 until arr.length()) {
        arr.optObject(i)?.let { roots += parseCategoryNode(it) }
    }
    return CategoryTree(
        categories = roots,
        totalMemories = obj.optInt("total_memories", roots.sumOf { it.totalCount }),
    )
}

/** Finds a category by its stable slash-separated path. */
fun findCategoryNode(roots: List<CategoryNode>, path: String): CategoryNode? {
    for (root in roots) {
        if (root.path == path) return root
        findCategoryNode(root.children, path)?.let { return it }
    }
    return null
}

/** Root-to-leaf chain for breadcrumb navigation. */
fun categoryBreadcrumb(roots: List<CategoryNode>, path: String): List<CategoryNode> {
    fun walk(nodes: List<CategoryNode>, acc: List<CategoryNode>): List<CategoryNode>? {
        for (node in nodes) {
            val next = acc + node
            if (node.path == path) return next
            walk(node.children, next)?.let { return it }
        }
        return null
    }
    return walk(roots, emptyList()).orEmpty()
}

fun MemoryItem.isInCategoryBranch(path: String): Boolean {
    val own = categoryPath ?: return false
    return own == path || own.startsWith("$path/")
}
