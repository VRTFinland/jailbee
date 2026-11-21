// Mirror of src/jailbee/web/protocol.py. Bump PROTOCOL_VERSION in both files
// on any change either side could misread.

export const PROTOCOL_VERSION = 1

export const CLOSE_UNAUTHORIZED = 4001
export const CLOSE_PROTOCOL_MISMATCH = 4002
export const CLOSE_BAD_MESSAGE = 4400

export interface ClientAuth {
    type: "auth"
    v: number
    token: string
}

export type ClientMessage = ClientAuth

export interface ContainerRow {
    name: string
}

export interface FrontPageView {
    seq: number
    gathered_at: string
    containers: ContainerRow[]
}

export interface ServerHello {
    type: "hello"
    v: number
    device_name: string
}

export interface FrontPage {
    type: "front_page"
    v: number
    view: FrontPageView
}

export interface Status {
    type: "status"
    v: number
    message: string | null
}

export type ServerMessage = ServerHello | FrontPage | Status
