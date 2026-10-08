package dev.reelmemory.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import dev.reelmemory.app.auth.AuthValidation
import dev.reelmemory.app.auth.SessionManager
import dev.reelmemory.app.auth.SignInResult
import kotlinx.coroutines.launch

/**
 * Sign-in gate shown when there is no session. Email/password only:
 * welcome (sign in / create account) -> form screens.
 *
 * [onSignedIn] fires after the session is established; the caller then
 * kicks off a sync so anything queued while signed out uploads.
 */
@Composable
fun AuthFlow(
    session: SessionManager,
    onSignedIn: () -> Unit,
    modifier: Modifier = Modifier
) {
    var screen by remember { mutableStateOf<AuthScreen>(AuthScreen.Welcome) }
    when (screen) {
        AuthScreen.Welcome -> WelcomeScreen(
            onSignIn = { screen = AuthScreen.SignIn },
            onSignUp = { screen = AuthScreen.SignUp },
            modifier = modifier
        )
        AuthScreen.SignIn -> EmailAuthScreen(
            title = "Sign in",
            submitLabel = "Sign in",
            onSubmit = { email, password -> session.login(email, password) },
            onBack = { screen = AuthScreen.Welcome },
            onDone = onSignedIn,
            showConfirm = false,
            modifier = modifier
        )
        AuthScreen.SignUp -> EmailAuthScreen(
            title = "Create account",
            submitLabel = "Create account",
            onSubmit = { email, password -> session.signUp(email, password) },
            onBack = { screen = AuthScreen.Welcome },
            onDone = onSignedIn,
            showConfirm = true,
            modifier = modifier
        )
    }
}

private sealed interface AuthScreen {
    data object Welcome : AuthScreen
    data object SignIn : AuthScreen
    data object SignUp : AuthScreen
}

@Composable
private fun WelcomeScreen(
    onSignIn: () -> Unit,
    onSignUp: () -> Unit,
    modifier: Modifier = Modifier
) {
    Column(
        modifier = modifier.fillMaxSize().padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            "Reel Memory",
            style = MaterialTheme.typography.headlineMedium,
            fontWeight = FontWeight.Bold
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "Your reels, understood. Ask questions, get to-dos, decide what deserves your time.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center
        )
        Spacer(Modifier.height(32.dp))
        Button(
            onClick = onSignUp,
            modifier = Modifier.fillMaxWidth()
        ) {
            Text("Create account")
        }
        Spacer(Modifier.height(12.dp))
        TextButton(onClick = onSignIn) {
            Text("I already have an account — sign in")
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun EmailAuthScreen(
    title: String,
    submitLabel: String,
    onSubmit: suspend (email: String, password: String) -> SignInResult,
    onBack: () -> Unit,
    onDone: () -> Unit,
    showConfirm: Boolean,
    modifier: Modifier = Modifier
) {
    var email by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var confirm by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    val scope = rememberCoroutineScope()

    val emailError = AuthValidation.validateEmail(email).takeIf { email.isNotEmpty() }
    val passwordError = AuthValidation.validatePassword(password).takeIf { password.isNotEmpty() }
    val canSubmit = AuthValidation.validateEmail(email) == null &&
        AuthValidation.validatePassword(password) == null &&
        (!showConfirm || password == confirm) &&
        !busy

    Scaffold(
        modifier = modifier,
        topBar = {
            TopAppBar(
                title = { Text(title) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(
            modifier = Modifier.padding(padding).fillMaxSize().padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp)
        ) {
            OutlinedTextField(
                value = email,
                onValueChange = { email = it; error = null },
                label = { Text("Email") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Email),
                isError = emailError != null,
                supportingText = emailError?.let { { Text(it) } },
                modifier = Modifier.fillMaxWidth()
            )
            OutlinedTextField(
                value = password,
                onValueChange = { password = it; error = null },
                label = { Text("Password") },
                singleLine = true,
                visualTransformation = PasswordVisualTransformation(),
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                isError = passwordError != null,
                supportingText = passwordError?.let { { Text(it) } }
                    ?: { Text("At least ${AuthValidation.MIN_PASSWORD_LEN} characters.") },
                modifier = Modifier.fillMaxWidth()
            )
            if (showConfirm) {
                OutlinedTextField(
                    value = confirm,
                    onValueChange = { confirm = it; error = null },
                    label = { Text("Confirm password") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    isError = confirm.isNotEmpty() && confirm != password,
                    supportingText = {
                        if (confirm.isNotEmpty() && confirm != password) Text("Passwords don't match.")
                    },
                    modifier = Modifier.fillMaxWidth()
                )
            }
            error?.let {
                Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            }
            Button(
                onClick = {
                    scope.launch {
                        busy = true
                        error = null
                        when (val r = onSubmit(email.trim(), password)) {
                            SignInResult.Ok -> onDone()
                            is SignInResult.Error -> error = r.message
                        }
                        busy = false
                    }
                },
                enabled = canSubmit,
                modifier = Modifier.fillMaxWidth()
            ) {
                if (busy) {
                    CircularProgressIndicator(
                        modifier = Modifier.height(20.dp).width(20.dp),
                        strokeWidth = 2.dp
                    )
                    Spacer(Modifier.width(8.dp))
                }
                Text(submitLabel)
            }
        }
    }
}
