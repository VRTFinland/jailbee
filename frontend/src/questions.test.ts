import { describe, expect, it } from 'vitest'
import { parseQuestionSet, initialAnswers, buildAnswerMessage } from './questions'

describe('question sets', () => {
  it('parses choices and builds recommended answers', () => {
    const set = parseQuestionSet('❓ **Q12** - **Deploy**: Choose one\na) Now\nb) Later\n➡️ b: safer')!
    expect(set.questions[0]).toMatchObject({ number: 12, title: 'Deploy', body: 'Choose one', options: [{ letter: 'a', text: 'Now' }, { letter: 'b', text: 'Later' }], recommendation: 'b' })
    expect(buildAnswerMessage(set, initialAnswers(set))).toBe('Q12: b')
  })

  it('handles multiple blocks, open answers, notes, and no-letter recommendations', () => {
    const source = 'Intro\n❓ **Q1** - **Proceed**: Continue?\n➡️ Recommended: proceed\n❓ **Q2** - **Why**: Explain\nmore detail'
    const set = parseQuestionSet(source)!
    expect(set.questions).toHaveLength(2)
    expect(set.questions[1]).toMatchObject({ body: 'Explain\nmore detail', options: [], recommendation: null })
    expect(source.slice(set.questions[1]!.start, set.questions[1]!.end)).toContain('❓ **Q2**')
    const answers = initialAnswers(set)
    expect(answers[0]!.value).toBe('yes')
    answers[0]!.note = 'if safe'
    answers[1]!.value = 'Because it works'
    expect(buildAnswerMessage(set, answers)).toBe('Q1: yes – if safe\nQ2: Because it works')
  })

  it('ignores fenced examples and rejects malformed or ambiguous blocks', () => {
    expect(parseQuestionSet('```md\n❓ **Q1** - **Example**: Sample\n```')).toBeNull()
    expect(parseQuestionSet('❓ **Q1** - **Valid**: Text\n❓ Q2: broken')).toBeNull()
    expect(parseQuestionSet('❓ **Q1** - **Valid**: Text\na) One\n➡️ b: missing')).toBeNull()
    expect(parseQuestionSet('❓ **Q1** - **One**: Text\n❓ **Q1** - **Two**: Text')).toBeNull()
    expect(parseQuestionSet('❓ **Q1** - **One**: Text\na) One\na) Duplicate')).toBeNull()
  })
})
