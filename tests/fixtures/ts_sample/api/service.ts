import { HttpClient } from "@angular/common/http";
import { format } from "../util";

const BASE = "/api";

export class NovelService {
    constructor(private http: HttpClient) {}

    /** Fetch a novel by id. */
    get(id: number): Novel {
        return this.http.get<Novel>(`${BASE}/novels/${id}`);
    }

    label(n: Novel): string {
        return format(n.title);
    }
}
