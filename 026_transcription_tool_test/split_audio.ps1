$input = "LSMzinas.mp3"
$segmentLength = 165 # # Segment duration (seconds)
$step = 150 # # Step size (overlap = 15 sec)

$duration = (ffprobe -v error -show_entries format=duration -of csv=p=0 $input)
$start = 0
$i = 0

while ($start -lt $duration) {
    ffmpeg -y -i $input -ss $start -t $segmentLength -c copy ("output_{0:D3}.mp3" -f $i)
    $start += $step
    $i++
}