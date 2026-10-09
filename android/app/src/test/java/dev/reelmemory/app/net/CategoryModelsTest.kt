package dev.reelmemory.app.net

import org.junit.Assert.*
import org.junit.Test

class CategoryModelsTest {
    private val payload = """
        {
          "categories": [
            {
              "id": "r1",
              "name": "Technology & AI",
              "slug": "technology-ai",
              "path": "technology-ai",
              "depth": 0,
              "direct_count": 0,
              "total_count": 3,
              "children": [
                {
                  "id": "r2",
                  "name": "Artificial Intelligence",
                  "slug": "artificial-intelligence",
                  "path": "technology-ai/artificial-intelligence",
                  "depth": 1,
                  "direct_count": 0,
                  "total_count": 3,
                  "children": [
                    {
                      "id": "r3",
                      "name": "Claude Code",
                      "slug": "claude-code",
                      "path": "technology-ai/artificial-intelligence/claude-code",
                      "depth": 2,
                      "direct_count": 3,
                      "total_count": 3,
                      "children": []
                    }
                  ]
                }
              ]
            }
          ],
          "total_memories": 3
        }
    """.trimIndent()

    @Test
    fun `tree parses recursively with counts`() {
        val tree = parseCategoryTree(payload)!!
        assertEquals(3, tree.totalMemories)
        assertEquals("Technology & AI", tree.categories.single().name)
        val ai = tree.categories.single().children.single()
        assertEquals(3, ai.totalCount)
        assertEquals("Claude Code", ai.children.single().name)
    }

    @Test
    fun `find and breadcrumb follow stable path`() {
        val roots = parseCategoryTree(payload)!!.categories
        val path = "technology-ai/artificial-intelligence/claude-code"
        assertEquals("Claude Code", findCategoryNode(roots, path)?.name)
        assertEquals(
            listOf("Technology & AI", "Artificial Intelligence", "Claude Code"),
            categoryBreadcrumb(roots, path).map { it.name }
        )
    }

    @Test
    fun `branch match includes descendants only`() {
        val item = MemoryItem(
            id = "m1",
            title = "Claude Code",
            summary = null,
            category = "Claude Code",
            categoryPath = "technology-ai/artificial-intelligence/claude-code",
            processingStatus = "READY",
            createdAt = null,
        )
        assertTrue(item.isInCategoryBranch("technology-ai"))
        assertTrue(item.isInCategoryBranch("technology-ai/artificial-intelligence"))
        assertFalse(item.isInCategoryBranch("business-work"))
    }

    @Test
    fun `malformed category payload fails closed`() {
        assertNull(parseCategoryTree("not json"))
        assertNull(parseCategoryTree("{}"))
    }
}
