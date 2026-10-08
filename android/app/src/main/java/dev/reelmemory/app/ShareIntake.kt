package dev.reelmemory.app

/**
 * Result of classifying raw URLs found in a shared text payload.
 *
 * [instagramUrls] are canonical Instagram URLs that follow the existing
 * reel/post capture flow (`POST /v1/captures`). [webUrls] are any other
 * http(s) URLs (websites, articles, threads, docs) that follow the new
 * web capture flow (`POST /v1/captures/url {url}`).
 */
data class ClassifiedShare(
    /** Canonical Instagram URLs (full canonicalize result kept for queueing). */
    val instagramUrls: List<UrlCanonicalizer.Result.Ok>,
    /** Any other http(s) URLs (websites, articles, docs) for web capture. */
    val webUrls: List<String>,
) {
    val isEmpty: Boolean get() = instagramUrls.isEmpty() && webUrls.isEmpty()
}

/** Pure share-intake helpers shared by [ShareReceiverActivity] and JVM tests. */
object ShareIntake {
    /**
     * Splits [urls] into Instagram captures vs. general web captures.
     * URLs that are neither are dropped.
     */
    fun classifyUrls(urls: List<String>): ClassifiedShare {
        val instagram = mutableListOf<UrlCanonicalizer.Result.Ok>()
        val web = mutableListOf<String>()
        for (url in urls) {
            when (val canonical = UrlCanonicalizer.canonicalize(url)) {
                is UrlCanonicalizer.Result.Ok -> instagram.add(canonical)
                is UrlCanonicalizer.Result.Err ->
                    if (isWebUrl(url)) web.add(normalizeWebUrl(url))
            }
        }
        return ClassifiedShare(
            instagramUrls = instagram.distinctBy { it.canonicalUrl },
            webUrls = web.distinct(),
        )
    }

    /** True for well-formed http/https URLs with a host — the web-capture candidates. */
    fun isWebUrl(url: String): Boolean {
        return try {
            val uri = java.net.URI(url)
            (uri.scheme == "http" || uri.scheme == "https") && !uri.host.isNullOrBlank()
        } catch (_: Exception) {
            false
        }
    }

    /** Trims whitespace; backend receives the URL as-is otherwise. */
    fun normalizeWebUrl(url: String): String = url.trim()
}
