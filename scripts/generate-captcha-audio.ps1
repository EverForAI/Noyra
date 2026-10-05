$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$assetDirectory = Join-Path $PSScriptRoot '../src/noyra/web/assets'
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speech.SelectVoice('Microsoft Huihui Desktop')
    $speech.Rate = -2
    $format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $words = @(0x96f6, 0x4e00, 0x4e8c, 0x4e09, 0x56db, 0x4e94, 0x516d, 0x4e03, 0x516b, 0x4e5d)
    foreach ($digit in 2..9) {
        $path = Join-Path $assetDirectory "captcha-zh-$digit.wav"
        $speech.SetOutputToWaveFile($path, $format)
        $speech.Speak([string][char]$words[$digit])
        $speech.SetOutputToNull()
    }
} finally {
    $speech.Dispose()
}
