package dev.reelmemory.app.auth

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import android.util.Log
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Where the session's token pair lives.
 */
interface TokenStore {
    fun save(pair: TokenPair)
    fun load(): TokenPair?
    fun clear()
}

/**
 * Token storage with the same security posture as EncryptedSharedPreferences:
 * the token pair is AES-256-GCM encrypted with a key that never leaves the
 * Android Keystore, and only ciphertext touches disk (plain SharedPreferences).
 *
 * Implemented directly against the SDK (android.security.keystore) instead of
 * pulling in androidx.security:security-crypto — identical guarantees, one
 * fewer dependency. If that library is ever added, this class is the swap
 * point (same interface).
 *
 * Note: on a device where the Keystore is unavailable/broken, load() returns
 * null and save() is a no-op — the user simply has to sign in again.
 */
class EncryptedTokenStore(context: Context) : TokenStore {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    override fun save(pair: TokenPair) {
        try {
            val key = getOrCreateKey() ?: return
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, key)
            val iv = cipher.iv
            val ciphertext = cipher.doFinal(pair.toJson().toString().toByteArray(Charsets.UTF_8))
            prefs.edit()
                .putString(KEY_IV, Base64.encodeToString(iv, Base64.NO_WRAP))
                .putString(KEY_DATA, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .apply()
        } catch (e: Exception) {
            Log.w(TAG, "could not encrypt tokens", e)
        }
    }

    override fun load(): TokenPair? {
        return try {
            val ivB64 = prefs.getString(KEY_IV, null) ?: return null
            val dataB64 = prefs.getString(KEY_DATA, null) ?: return null
            val key = getKey() ?: return null
            val cipher = Cipher.getInstance(TRANSFORMATION)
            val spec = GCMParameterSpec(GCM_TAG_BITS, Base64.decode(ivB64, Base64.NO_WRAP))
            cipher.init(Cipher.DECRYPT_MODE, key, spec)
            val plain = cipher.doFinal(Base64.decode(dataB64, Base64.NO_WRAP))
            TokenPair.fromStoredJson(plain.toString(Charsets.UTF_8))
        } catch (e: Exception) {
            // Wrong key, corrupted prefs, tampered data: treat as signed out.
            Log.w(TAG, "could not decrypt tokens; clearing", e)
            clear()
            null
        }
    }

    override fun clear() {
        prefs.edit().remove(KEY_IV).remove(KEY_DATA).apply()
    }

    private fun getOrCreateKey(): SecretKey? = try {
        getKey() ?: run {
            val keyGen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
            val spec = KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build()
            keyGen.init(spec)
            keyGen.generateKey()
        }
    } catch (e: Exception) {
        Log.w(TAG, "keystore unavailable", e)
        null
    }

    private fun getKey(): SecretKey? = try {
        val ks = KeyStore.getInstance(KEYSTORE)
        ks.load(null)
        (ks.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.secretKey
    } catch (e: Exception) {
        Log.w(TAG, "keystore unavailable", e)
        null
    }

    companion object {
        private const val TAG = "EncryptedTokenStore"
        private const val PREFS_NAME = "reel_memory_tokens"
        private const val KEY_IV = "iv"
        private const val KEY_DATA = "data"
        private const val KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "reel_memory_token_key"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val GCM_TAG_BITS = 128
    }
}
