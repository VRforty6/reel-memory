package dev.reelmemory.app.net

import dev.reelmemory.app.data.SettingsStore
import dev.reelmemory.app.json.JsonObject
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/** Metadata returned by the self-hosted update endpoint. */
data class AppUpdateInfo(
    val updateAvailable: Boolean,
    val latestVersionCode: Int,
    val latestVersionName: String,
    val releaseNotes: String?,
    val sizeBytes: Long,
    val sha256: String,
    val downloadPath: String,
)


fun parseAppUpdateInfo(payload: String): AppUpdateInfo? {
    val obj = try { JsonObject(payload) } catch (_: Exception) { return null }
    val path = obj.optString("download_path", "")
    if (!path.startsWith("/") || "://" in path) return null
    val code = obj.optInt("latest_version_code", 0)
    val name = obj.optString("latest_version_name", "")
    val size = obj.optLong("size_bytes", 0L)
    val sha = obj.optString("sha256", "").lowercase()
    if (code < 1 || name.isBlank() || size < 1 || !sha.matches(Regex("[0-9a-f]{64}"))) return null
    return AppUpdateInfo(
        updateAvailable = obj.optBoolean("update_available", false),
        latestVersionCode = code,
        latestVersionName = name,
        releaseNotes = obj.optStringOrNull("release_notes")
            ?.takeIf { !obj.isNull("release_notes") && it.isNotBlank() },
        sizeBytes = size,
        sha256 = sha,
        downloadPath = path,
    )
}

sealed interface UpdateCheckResult {
    data class Ok(val info: AppUpdateInfo) : UpdateCheckResult
    data class Unavailable(val message: String) : UpdateCheckResult
}

class UpdateApi(private val settings: SettingsStore) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .callTimeout(180, TimeUnit.SECONDS)
        .build()

    suspend fun check(currentVersionCode: Long): UpdateCheckResult = withContext(Dispatchers.IO) {
        val base = try { settings.getBackendUrl() } catch (_: Exception) {
            return@withContext UpdateCheckResult.Unavailable("Cannot read backend URL")
        }
        val request = Request.Builder()
            .url("$base/v1/app/update?current_version_code=$currentVersionCode")
            .get()
            .header("Accept", "application/json")
            .build()
        try {
            client.newCall(request).execute().use { response ->
                val body = response.body?.string().orEmpty()
                if (response.code != 200) {
                    return@withContext UpdateCheckResult.Unavailable(
                        if (response.code == 503) "Updates are not configured on this server"
                        else "Update check returned HTTP ${response.code}"
                    )
                }
                val parsed = parseAppUpdateInfo(body)
                    ?: return@withContext UpdateCheckResult.Unavailable("Invalid update metadata")
                UpdateCheckResult.Ok(parsed)
            }
        } catch (e: Exception) {
            UpdateCheckResult.Unavailable(e.message ?: e.javaClass.simpleName)
        }
    }

    /** Downloads to app-private cache and verifies byte count + SHA-256. */
    suspend fun download(info: AppUpdateInfo, destination: File): File = withContext(Dispatchers.IO) {
        require(info.sizeBytes in 1..200_000_000L) { "Invalid update size" }
        require(info.sha256.matches(Regex("[0-9a-f]{64}"))) { "Invalid update checksum" }
        val base = settings.getBackendUrl()
        destination.parentFile?.mkdirs()
        val partial = File(destination.parentFile, destination.name + ".part")
        partial.delete()
        val request = Request.Builder().url("$base${info.downloadPath}").get().build()
        try {
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) error("Download returned HTTP ${response.code}")
                val body = response.body ?: error("Empty update response")
                val digest = MessageDigest.getInstance("SHA-256")
                var written = 0L
                body.byteStream().use { input ->
                    partial.outputStream().use { output ->
                        val buffer = ByteArray(64 * 1024)
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            written += count
                            if (written > 200_000_000L) error("Update exceeds download limit")
                            digest.update(buffer, 0, count)
                            output.write(buffer, 0, count)
                        }
                    }
                }
                if (written != info.sizeBytes) error("Update download is incomplete")
                val actual = digest.digest().joinToString("") { "%02x".format(it) }
                if (!actual.equals(info.sha256, ignoreCase = true)) error("Update checksum mismatch")
            }
            destination.delete()
            if (!partial.renameTo(destination)) error("Could not finalize downloaded update")
            destination
        } catch (e: Exception) {
            partial.delete()
            throw e
        }
    }
}
