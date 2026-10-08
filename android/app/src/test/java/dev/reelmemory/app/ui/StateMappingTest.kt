package dev.reelmemory.app.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class StateMappingTest {

    // ---- detail routing ---------------------------------------------------

    @Test
    fun detailRoute_ready() {
        assertEquals(DetailRoute.READY, detailRouteFor("READY"))
    }

    @Test
    fun detailRoute_metadataOnly() {
        assertEquals(DetailRoute.METADATA_ONLY, detailRouteFor("METADATA_ONLY"))
    }

    @Test
    fun detailRoute_failedVariants() {
        assertEquals(DetailRoute.FAILED, detailRouteFor("FAILED"))
        assertEquals(DetailRoute.FAILED, detailRouteFor("FAILED_TRANSCRIPTION"))
        assertEquals(DetailRoute.FAILED, detailRouteFor("SOURCE_UNAVAILABLE"))
    }

    @Test
    fun detailRoute_processingStages() {
        assertEquals(DetailRoute.PROCESSING, detailRouteFor("QUEUED"))
        assertEquals(DetailRoute.PROCESSING, detailRouteFor("TRANSCRIBING"))
        assertEquals(DetailRoute.PROCESSING, detailRouteFor("INDEXING"))
    }

    @Test
    fun detailRoute_unknownStatusDegradesToProcessing() {
        // A new backend stage must never be mislabeled as failed or ready.
        assertEquals(DetailRoute.PROCESSING, detailRouteFor("SOMETHING_NEW"))
        assertEquals(DetailRoute.PROCESSING, detailRouteFor(""))
    }

    @Test
    fun allowsQuestionTabs_onlyReady() {
        assertTrue(allowsQuestionTabs(DetailRoute.READY))
        assertFalse(allowsQuestionTabs(DetailRoute.PROCESSING))
        assertFalse(allowsQuestionTabs(DetailRoute.METADATA_ONLY))
        assertFalse(allowsQuestionTabs(DetailRoute.FAILED))
    }

    // ---- stage labels -----------------------------------------------------

    @Test
    fun stageLabels_honestCopy() {
        assertEquals("Uploaded", stageLabel("CAPTURED"))
        assertEquals("Uploaded", stageLabel("QUEUED"))
        assertEquals("Finding media", stageLabel("RESOLVING_SOURCE"))
        assertEquals("Extracting media", stageLabel("MEDIA_READY"))
        assertEquals("Transcribing speech", stageLabel("TRANSCRIBING"))
        assertEquals("Understanding visuals", stageLabel("ANALYZING_VISUALS"))
        assertEquals("Reading on-screen text", stageLabel("RUNNING_OCR"))
        assertEquals("Building memory", stageLabel("GENERATING_MEMORY"))
        assertEquals("Indexing", stageLabel("INDEXING"))
        assertEquals("Ready", stageLabel("READY"))
    }

    @Test
    fun stageLabels_caseInsensitiveAndUnknown() {
        assertEquals("Transcribing speech", stageLabel("transcribing"))
        assertEquals("Working on it", stageLabel(null))
        assertEquals("Working on it", stageLabel("BOGUS_STAGE"))
    }

    @Test
    fun stagePosition_ordering() {
        assertEquals(0, stagePosition("CAPTURED"))
        assertEquals(0, stagePosition("QUEUED"))
        assertEquals(3, stagePosition("TRANSCRIBING"))
        assertEquals(7, stagePosition("INDEXING"))
        assertEquals(-1, stagePosition("READY"))
        assertEquals(-1, stagePosition(null))
        assertEquals(-1, stagePosition("BOGUS"))
    }

    // ---- failure classification -------------------------------------------

    @Test
    fun retryable_failures() {
        listOf(
            "SOURCE_RATE_LIMITED", "SOURCE_RESOLUTION_FAILED", "MEDIA_DOWNLOAD_FAILED",
            "TRANSCRIPTION_FAILED", "VISION_FAILED", "OCR_FAILED", "EMBEDDING_FAILED",
            "VISUAL_INDEX_FAILED", "DATABASE_FAILED", "INDEXING_FAILED"
        ).forEach { code ->
            assertTrue("expected retryable: $code", isRetryableFailure(code))
            assertTrue("expected retryable lowercase: $code", isRetryableFailure(code.lowercase()))
        }
    }

    @Test
    fun permanent_failures() {
        listOf(
            "INVALID_URL", "UNSUPPORTED_SOURCE", "SOURCE_DELETED",
            "SOURCE_PRIVATE", "SOURCE_LOGIN_REQUIRED", "MEDIA_DECODE_FAILED", null, ""
        ).forEach { code ->
            assertFalse("expected permanent: $code", isRetryableFailure(code))
        }
    }

    @Test
    fun failureCopy_humanText() {
        assertEquals("We couldn't download the media.", failureCopy("MEDIA_DOWNLOAD_FAILED"))
        assertEquals("We couldn't download the media.", failureCopy("SOURCE_RESOLUTION_FAILED"))
        assertEquals("The original was deleted.", failureCopy("SOURCE_DELETED"))
        assertEquals("The original isn't publicly accessible.", failureCopy("SOURCE_PRIVATE"))
        assertEquals("The original isn't publicly accessible.", failureCopy("SOURCE_LOGIN_REQUIRED"))
        assertEquals("The file couldn't be read.", failureCopy("MEDIA_DECODE_FAILED"))
        assertEquals("Analysis hit a snag.", failureCopy("TRANSCRIPTION_FAILED"))
        assertEquals("Analysis hit a snag.", failureCopy("VISION_FAILED"))
        assertEquals("Analysis hit a snag.", failureCopy("OCR_FAILED"))
        assertEquals("Analysis hit a snag.", failureCopy("EMBEDDING_FAILED"))
        assertEquals("Analysis hit a snag.", failureCopy("VISUAL_INDEX_FAILED"))
        assertEquals("Analysis hit a snag.", failureCopy("INDEXING_FAILED"))
        assertEquals("That link isn't supported yet.", failureCopy("INVALID_URL"))
        assertEquals("That link isn't supported yet.", failureCopy("UNSUPPORTED_SOURCE"))
        assertEquals("A server hiccup interrupted this.", failureCopy("DATABASE_FAILED"))
        assertEquals("We couldn't analyze this item.", failureCopy(null))
        assertEquals("We couldn't analyze this item.", failureCopy("SOME_NEW_CODE"))
    }

    // ---- search rendering helpers ------------------------------------------

    @Test
    fun whyMatch_labels() {
        assertEquals(WhyMatch("👁", "Visual match"), whyMatch("VISUAL"))
        assertEquals(WhyMatch("🎙", "Transcript"), whyMatch("SPEECH"))
        assertEquals(WhyMatch("🔤", "On-screen text"), whyMatch("OCR"))
        assertEquals(WhyMatch("✦", "Caption match"), whyMatch("CAPTION"))
        assertEquals(WhyMatch("🏷", "Tag"), whyMatch("TAG"))
        assertEquals(WhyMatch("👁", "Visual match"), whyMatch("visual"))
        assertEquals(WhyMatch("✦", "Match"), whyMatch("BOGUS"))
    }

    @Test
    fun evidenceTimeSuffix_format() {
        assertEquals("· 00:18", evidenceTimeSuffix(18_000))
        assertEquals("· 01:01", evidenceTimeSuffix(61_000))
        assertEquals("· 1:02:03", evidenceTimeSuffix(3_723_000))
        assertEquals("· 00:00", evidenceTimeSuffix(0))
    }

    @Test
    fun evidenceTimeSuffix_absent() {
        assertNull(evidenceTimeSuffix(null))
        assertNull(evidenceTimeSuffix(-1))
    }

    @Test
    fun platformDisplayName_mapping() {
        assertEquals("Instagram", platformDisplayName("INSTAGRAM"))
        assertEquals("Instagram", platformDisplayName("instagram"))
        assertEquals("TikTok", platformDisplayName("TIKTOK"))
        assertEquals("YouTube", platformDisplayName("YOUTUBE"))
        assertEquals("Unknown source", platformDisplayName(null))
        assertEquals("Pixelfed", platformDisplayName("Pixelfed"))
    }

    @Test
    fun searchResultTitle_fallbackChain() {
        assertEquals("My reel", searchResultTitle("My reel", "someone", "INSTAGRAM"))
        assertEquals("Post by @someone", searchResultTitle(null, "someone", "INSTAGRAM"))
        assertEquals("Post by @someone", searchResultTitle("  ", "someone", "INSTAGRAM"))
        assertEquals("Saved Instagram post", searchResultTitle(null, null, "INSTAGRAM"))
        assertEquals("Saved Instagram post", searchResultTitle(null, " ", "instagram"))
        assertEquals("Saved memory", searchResultTitle(null, null, null))
        assertEquals("Saved memory", searchResultTitle(" ", " ", " "))
    }

    @Test
    fun libraryTitleFallback_noHandle() {
        assertEquals("My reel", libraryTitleFallback("My reel", "INSTAGRAM"))
        assertEquals("Saved TikTok post", libraryTitleFallback(null, "TIKTOK"))
        assertEquals("Saved memory", libraryTitleFallback(null, null))
    }

    @Test
    fun searchResultSnippet_prefersEvidence() {
        assertEquals("the snippet", searchResultSnippet("the snippet", "the summary"))
        assertEquals("the summary", searchResultSnippet(null, "the summary"))
        assertEquals("the summary", searchResultSnippet("  ", "the summary"))
        assertEquals("", searchResultSnippet(null, null))
    }

    // ---- library filters ----------------------------------------------------

    @Test
    fun libraryFilter_allAndReady() {
        assertTrue(LibraryFilter.ALL.matches("READY", "video"))
        assertTrue(LibraryFilter.ALL.matches("METADATA_ONLY", null))
        assertTrue(LibraryFilter.READY.matches("READY", null))
        assertTrue(LibraryFilter.READY.matches("READY", "video"))
        assertFalse(LibraryFilter.READY.matches("METADATA_ONLY", "article"))
        assertFalse(LibraryFilter.READY.matches("TRANSCRIBING", "video"))
    }

    @Test
    fun libraryFilter_mediaKinds() {
        assertTrue(LibraryFilter.VIDEOS.matches("READY", "video"))
        assertFalse(LibraryFilter.VIDEOS.matches("READY", "image"))
        assertFalse(LibraryFilter.VIDEOS.matches("READY", null))

        assertTrue(LibraryFilter.IMAGES.matches("READY", "image"))
        assertTrue(LibraryFilter.IMAGES.matches("READY", "album"))
        assertFalse(LibraryFilter.IMAGES.matches("READY", "video"))
        assertFalse(LibraryFilter.IMAGES.matches("READY", null))

        assertTrue(LibraryFilter.LINKS.matches("READY", "article"))
        assertTrue(LibraryFilter.LINKS.matches("METADATA_ONLY", null))
        assertTrue(LibraryFilter.LINKS.matches("METADATA_ONLY", "article"))
        assertFalse(LibraryFilter.LINKS.matches("READY", "video"))
        assertFalse(LibraryFilter.LINKS.matches("READY", null))
    }

    @Test
    fun libraryFilter_nullMediaKindOnlyAllAndReady() {
        assertTrue(LibraryFilter.ALL.matches("READY", null))
        assertTrue(LibraryFilter.READY.matches("READY", null))
        assertFalse(LibraryFilter.VIDEOS.matches("READY", null))
        assertFalse(LibraryFilter.IMAGES.matches("READY", null))
        assertFalse(LibraryFilter.LINKS.matches("READY", null))
    }

    // ---- saved date ----------------------------------------------------------

    @Test
    fun formatSavedDate_iso() {
        assertEquals("Sep 22, 2026", formatSavedDate("2026-09-22T10:00:00Z"))
        assertEquals("Jan 5, 2025", formatSavedDate("2025-01-05"))
    }

    @Test
    fun formatSavedDate_badInput() {
        assertEquals("", formatSavedDate(null))
        assertEquals("", formatSavedDate("  "))
        assertEquals("not-a-date", formatSavedDate("not-a-date"))
    }

    // ---- FAILED_RETRYABLE: internal automatic retry ------------------------

    @Test
    fun detailRoute_failedRetryableGoesToProcessing() {
        // Automatic backend retry renders the PROCESSING UI, never the
        // failed screen (which offers a manual Try Again).
        assertEquals(DetailRoute.PROCESSING, detailRouteFor("FAILED_RETRYABLE"))
        // Ordinary failures are unaffected.
        assertEquals(DetailRoute.FAILED, detailRouteFor("FAILED"))
        assertEquals(DetailRoute.FAILED, detailRouteFor("FAILED_TRANSCRIPTION"))
        assertEquals(DetailRoute.FAILED, detailRouteFor("SOURCE_UNAVAILABLE"))
    }

    @Test
    fun isAutoRetrying_flags() {
        assertTrue(isAutoRetrying("FAILED_RETRYABLE"))
        assertFalse(isAutoRetrying("FAILED"))
        assertFalse(isAutoRetrying("TRANSCRIBING"))
        assertFalse(isAutoRetrying(null))
    }

    // ---- processing intro copy: retry + source-aware -------------------------

    @Test
    fun processingIntroCopy_autoRetry() {
        val copy = processingIntroCopy("FAILED_RETRYABLE", "video")
        assertTrue(copy.contains("Retrying automatically"))
        // Retry honesty wins over the source wording.
        assertTrue(processingIntroCopy("FAILED_RETRYABLE", "article").contains("Retrying automatically"))
    }

    @Test
    fun processingIntroCopy_linkCapture() {
        assertEquals(
            "Your link is saved. Checking whether media is available.",
            processingIntroCopy("TRANSCRIBING", "article")
        )
        assertEquals(
            "Your link is saved. Checking whether media is available.",
            processingIntroCopy("RESOLVING_SOURCE", "article")
        )
    }

    @Test
    fun processingIntroCopy_fileUpload() {
        val upload = processingIntroCopy("TRANSCRIBING", "video")
        assertTrue(upload.contains("Your upload is safe"))
        assertTrue(processingIntroCopy("ANALYZING_VISUALS", "image").contains("Your upload is safe"))
        assertTrue(processingIntroCopy("GENERATING_MEMORY", "album").contains("Your upload is safe"))
        // Unknown source keeps the previous upload wording (no regression).
        assertTrue(processingIntroCopy("QUEUED", null).contains("Your upload is safe"))
    }

    // ---- media vs link-card split ---------------------------------------------

    @Test
    fun isMediaItem_split() {
        assertTrue(isMediaItem("video"))
        assertTrue(isMediaItem("image"))
        assertTrue(isMediaItem("album"))
        assertFalse(isMediaItem("article"))
        assertFalse(isMediaItem(null))
        assertFalse(isMediaItem("unknown"))
    }

    // ---- thumbnail skip logic ---------------------------------------------------

    @Test
    fun shouldSkipThumbnail_rules() {
        // Backend says no thumbnail: never request.
        assertTrue(shouldSkipThumbnail(false, false))
        assertTrue(shouldSkipThumbnail(false, true))
        // Session already 404'd: never re-request.
        assertTrue(shouldSkipThumbnail(null, true))
        // Flag present and true: request as normal.
        assertFalse(shouldSkipThumbnail(true, false))
        // Flag absent, never 404'd: request as normal.
        assertFalse(shouldSkipThumbnail(null, false))
    }
}
