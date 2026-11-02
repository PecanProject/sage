import { obj } from "@/lib/utils"

type FieldKind =
  | { kind: "enum"; options: string[] }
  | { kind: "integer"; min?: number; max?: number }
  | { kind: "number"; min?: number; max?: number }
  | { kind: "boolean" }
  | { kind: "text" }

export type FieldSpec = {
  name: string
  title: string
  description: string
  group: string
  /** "manage" for a field the Manage page edits rather than Settings: providers, the model list and the defaults. */
  page: string | null
  kind: FieldKind
  /** The schema default; a value equal to it needs no reset. */
  default: unknown
}

function str(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined
}

function num(v: unknown): number | undefined {
  return typeof v === "number" ? v : undefined
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []
}

function kindOf(prop: Record<string, unknown>, defs: Record<string, unknown>): FieldKind {
  const variants = Array.isArray(prop.anyOf) ? prop.anyOf.map(obj).filter((v) => v.type !== "null") : [prop]
  let base = variants[0] ?? prop
  const ref = str(base.$ref)
  if (ref) base = obj(defs[ref.split("/").pop() ?? ""])
  const options = strings(base.enum)
  if (options.length > 0) return { kind: "enum", options }
  const min = num(base.minimum) ?? num(base.exclusiveMinimum)
  const max = num(base.maximum) ?? num(base.exclusiveMaximum)
  if (base.type === "integer") return { kind: "integer", min, max }
  if (base.type === "number") return { kind: "number", min, max }
  if (base.type === "boolean") return { kind: "boolean" }
  return { kind: "text" }
}

export function fieldsFromSchema(schema: unknown): FieldSpec[] {
  const root = obj(schema)
  const props = obj(root.properties)
  const defs = obj(root.$defs)
  return Object.entries(props).map(([name, raw]) => {
    const prop = obj(raw)
    return {
      name,
      title: str(prop.title) ?? name,
      description: str(prop.description) ?? "",
      group: str(prop.group) ?? "Other",
      page: str(prop.page) ?? null,
      kind: kindOf(prop, defs),
      default: prop.default ?? null,
    }
  })
}

/** Each settings group's description, from the schema's `groups`. */
export function groupsFromSchema(schema: unknown): Record<string, string> {
  return Object.fromEntries(
    Object.entries(obj(obj(schema).groups)).flatMap(([name, text]) =>
      typeof text === "string" ? [[name, text]] : [],
    ),
  )
}
