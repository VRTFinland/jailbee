export interface Question {
  number: number
  title: string
  body: string
  options: { letter: string; text: string }[]
  recommendation: string | null
  start: number
  end: number
}

export interface QuestionSet { questions: Question[] }
export interface Answer { value: string; note: string }

export function parseQuestionSet(text: string): QuestionSet | null {
  const lines = text.match(/[^\n]*(?:\n|$)/g)?.filter(Boolean) ?? []
  const questions: Question[] = []
  let current: Question | null = null
  let offset = 0
  let fence: { char: string; length: number } | null = null
  let invalid = false
  for (const raw of lines) {
    const line = raw.replace(/\r?\n$/, '')
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/)
    if (marker) {
      if (!fence) fence = { char: marker[1]![0]!, length: marker[1]!.length }
      else if (marker[1]![0] === fence.char && marker[1]!.length >= fence.length && /^ {0,3}(?:`+|~+)\s*$/.test(line)) fence = null
      offset += raw.length
      continue
    }
    if (fence) { offset += raw.length; continue }
    if (/[\x00-\x08\x0b-\x1f\x7f]/.test(line)) { invalid = true; break }
    if (/^\s*❓/.test(line)) {
      const header = line.match(/^❓ \*\*Q([1-9]\d*)\*\* - \*\*(.+?)\*\*:\s*(.*)$/)
      if (!header || !header[2]!.trim() || !Number.isSafeInteger(Number(header[1])) || questions.some(q => q.number === Number(header[1]))) { invalid = true; break }
      if (current) current.end = offset
      current = { number: Number(header[1]), title: header[2]!, body: header[3]!, options: [], recommendation: null, start: offset, end: text.length }
      questions.push(current)
    } else if (current) {
      const option = line.match(/^([a-z])\)\s+(.+)$/)
      const recommendation = line.match(/^➡️\s*(.*)$/)
      if (option) {
        if (!option[2]!.trim() || current.recommendation !== null || current.options.some(o => o.letter === option[1])) { invalid = true; break }
        current.options.push({ letter: option[1]!, text: option[2]! })
      } else if (/^[A-Za-z]\)/.test(line)) {
        invalid = true; break
      } else if (recommendation) {
        if (current.recommendation !== null || !recommendation[1]!.trim()) { invalid = true; break }
        const letter = recommendation[1]!.match(/^([a-z])\s*:/)
        current.recommendation = letter ? letter[1]! : 'yes'
      } else if (current.options.length === 0 && current.recommendation === null) {
        current.body += (current.body ? '\n' : '') + line
      }
    }
    offset += raw.length
  }
  for (const q of questions) {
    q.body = q.body.trim()
    if (q.recommendation && q.recommendation !== 'yes' && !q.options.some(o => o.letter === q.recommendation)) invalid = true
  }
  return invalid || !questions.length ? null : { questions }
}

export function initialAnswers(set: QuestionSet): Answer[] {
  return set.questions.map(q => ({ value: q.recommendation ?? '', note: '' }))
}

export function buildAnswerMessage(set: QuestionSet, answers: Answer[]): string {
  return set.questions.map((q, i) => {
    const answer = answers[i]
    const value = (answer?.value ?? '').replace(/[\r\n]+/g, ' ')
    const note = (answer?.note ?? '').replace(/[\r\n]+/g, ' ')
    return `Q${q.number}: ${value}${note ? ` – ${note}` : ''}`
  }).join('\n')
}
